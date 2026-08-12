from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.api.permissions import IsProjectAccessible
from core.api.serializers import ProjectDetailSerializer, ProjectMembershipSerializer, ProjectSerializer
from core.models import Profile, Project, ProjectMembership
from core.views import _is_admin_user


class ProjectViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only for now — create/edit/delete land in a later milestone
    alongside the rest of the admin/project-lead management screens.
    """
    permission_classes = [IsAuthenticated, IsProjectAccessible]

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectDetailSerializer
        return ProjectSerializer

    def get_queryset(self):
        user = self.request.user
        profile, _ = Profile.objects.get_or_create(user=user)

        # Mirrors core.views.projects()'s queryset so the app and the web
        # dashboard always agree on which projects a user can see.
        if profile.role == 'admin':
            qs = Project.objects.all()
        else:
            qs = Project.objects.filter(
                Q(members=user) | Q(project_lead=user) | Q(created_by=user)
            ).distinct()

        return qs.prefetch_related('members').annotate(
            total_tasks=Count('task', filter=Q(task__is_archived=False), distinct=True),
            done_tasks=Count('task', filter=Q(task__status='done', task__is_archived=False), distinct=True),
        )

    @action(detail=True, methods=['get'])
    def members(self, request, pk=None):
        project = self.get_object()
        memberships = ProjectMembership.objects.filter(project=project).select_related('user')
        return Response(ProjectMembershipSerializer(memberships, many=True).data)
