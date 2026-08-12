from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from core.api.serializers import TeamSerializer
from core.models import Profile, Team
from django.db.models import Q


class TeamViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only — team creation/editing stays a web-only admin/lead workflow
    for now. Scoped to teams whose project the requesting user can see, same
    rule as ProjectViewSet.get_queryset.
    """
    serializer_class = TeamSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        profile, _ = Profile.objects.get_or_create(user=user)

        if profile.role == 'admin':
            qs = Team.objects.all()
        else:
            qs = Team.objects.filter(
                Q(project__members=user) | Q(project__project_lead=user) | Q(project__created_by=user)
            ).distinct()

        project_id = self.request.query_params.get('project')
        if project_id:
            qs = qs.filter(project_id=project_id)
        return qs.select_related('lead', 'project').prefetch_related('members')
