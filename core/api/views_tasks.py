from django.db.models import Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.api.permissions import CanViewTask
from core.api.serializers import CommentSerializer, TaskCreateSerializer, TaskDetailSerializer, TaskListSerializer
from core.models import Profile, Task
from core.notifications import NotificationService
from core.views import (
    _log_task_activity,
    _status_label,
    _task_allowed_next_statuses,
    _task_can_transition,
)


class TaskViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """List/retrieve, plus the transition/comment actions below, plus create
    (any authenticated user can attempt it — TaskCreateSerializer enforces
    the same project-access + assignee-membership rules create_task does in
    core/views.py). Update/delete land in a later milestone.
    """
    permission_classes = [IsAuthenticated, CanViewTask]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return TaskDetailSerializer
        if self.action == 'create':
            return TaskCreateSerializer
        return TaskListSerializer

    def get_queryset(self):
        user = self.request.user
        profile, _ = Profile.objects.get_or_create(user=user)
        role = profile.role

        # Mirrors core.views.tasks()'s role-based queryset so the app and the
        # web dashboard always agree on which tasks a user can see.
        if role == 'admin':
            qs = Task.objects.select_related('project', 'assigned_to')
        elif role == 'project_lead':
            qs = Task.objects.select_related('project', 'assigned_to').filter(
                Q(project__projectmembership__user=user) | Q(project__project_lead=user)
            ).distinct()
        else:
            qs = Task.objects.select_related('project', 'assigned_to').filter(
                Q(project__members=user) | Q(assigned_to=user)
            ).distinct()

        project_id = self.request.query_params.get('project')
        task_status = self.request.query_params.get('status')
        assigned_to_id = self.request.query_params.get('assigned_to')
        if project_id:
            qs = qs.filter(project_id=project_id)
        if task_status:
            qs = qs.filter(status=task_status)
        if assigned_to_id:
            qs = qs.filter(assigned_to_id=assigned_to_id)
        return qs

    @action(detail=True, methods=['post'])
    def transition(self, request, pk=None):
        """Mirrors core.views.api_transition_task's business logic exactly."""
        task = self.get_object()
        new_status = (request.data.get('new_status') or '').strip()
        if not new_status:
            return Response({'error': 'new_status required'}, status=status.HTTP_400_BAD_REQUEST)

        if not _task_can_transition(request.user, task, new_status):
            return Response({'error': 'Unauthorized transition'}, status=status.HTTP_403_FORBIDDEN)

        old_status = task.status
        if old_status == new_status:
            return Response({
                'status': 'no_change',
                'old': old_status,
                'new': new_status,
                'allowed_next_statuses': _task_allowed_next_statuses(request.user, task),
            })

        from django.contrib.auth.models import User
        from django.db import transaction

        with transaction.atomic():
            assigned_old = task.assigned_to

            if new_status == 'in_review' and task.project_id:
                testers = User.objects.filter(
                    projectmembership__project=task.project,
                    projectmembership__role__in=['tester', 'qa'],
                ).distinct()
                task.assigned_to = testers.first() if testers.exists() else task.project.project_lead

            task._updated_by = request.user
            task.status = new_status
            task.save(update_fields=['status', 'assigned_to'])

            _log_task_activity(
                task=task, user=request.user, action='Status changed',
                old_value=_status_label(old_status), new_value=_status_label(new_status),
            )
            if assigned_old != task.assigned_to:
                _log_task_activity(
                    task=task, user=request.user, action='Assigned changed',
                    old_value=getattr(assigned_old, 'username', 'Unassigned'),
                    new_value=getattr(task.assigned_to, 'username', 'Unassigned'),
                )
            try:
                NotificationService().notify_status_change(task, old_status, new_status, request.user)
            except Exception:
                pass

        return Response({
            'status': 'success',
            'old': old_status,
            'new': new_status,
            'assigned_to': task.assigned_to.username if task.assigned_to else None,
            'allowed_next_statuses': _task_allowed_next_statuses(request.user, task),
        })

    @action(detail=True, methods=['get', 'post'])
    def comments(self, request, pk=None):
        task = self.get_object()
        if request.method == 'POST':
            text = (request.data.get('text') or '').strip()
            if not text:
                return Response({'error': 'text required'}, status=status.HTTP_400_BAD_REQUEST)
            comment = task.comments.create(user=request.user, text=text)
            return Response(CommentSerializer(comment).data, status=status.HTTP_201_CREATED)

        return Response(CommentSerializer(task.comments.select_related('user').all(), many=True).data)
