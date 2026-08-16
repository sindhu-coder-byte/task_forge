from django.db.models import Count, Q
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.api.permissions import CanCreateProject, IsProjectAccessible
from core.api.serializers import (
    ProjectCreateSerializer,
    ProjectDetailSerializer,
    ProjectMembershipSerializer,
    ProjectSerializer,
)
from core.models import Profile, Project, ProjectMembership
from core.views import _is_admin_user


class ProjectViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """List/retrieve are open to anyone with access; create is gated to
    admin/project_lead/visitor (CanCreateProject, mirrors create_project's
    @role_required in core/views.py). Update/delete land in a later
    milestone alongside the rest of the admin/project-lead management
    screens.
    """
    permission_classes = [IsAuthenticated, IsProjectAccessible]

    def get_permissions(self):
        if self.action == 'create':
            return [IsAuthenticated(), CanCreateProject()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ProjectDetailSerializer
        if self.action == 'create':
            return ProjectCreateSerializer
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
