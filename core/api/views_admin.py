"""Admin-only endpoints: user management, pipeline visibility, IT
departments/teams, and stage sequencing. All gated by IsAdminUser — mirrors
core.views' equivalent api_* functions (see core/urls.py's admin section)
rather than reinventing the validation rules.
"""
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from core.api.permissions import IsAdminUser
from core.models import (
    DepartmentPipelineSettings,
    ITDepartment,
    ITTeam,
    Profile,
    ProjectStageConfiguration,
)

_PIPELINE_STAGES = DepartmentPipelineSettings.STAGE_CHOICES
_DEPARTMENT_CHOICES = dict(DepartmentPipelineSettings.DEPARTMENT_CHOICES)


def _pipeline_map(department: str) -> dict:
    rows = DepartmentPipelineSettings.objects.filter(department=department)
    saved = {r.stage_key: r.is_visible for r in rows}
    return {key: saved.get(key, True) for key, _ in _PIPELINE_STAGES}


class UserListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        provider = request.query_params.get('provider')
        qs = Profile.objects.select_related('user')
        if provider == 'google':
            qs = qs.filter(oauth_provider__iexact='google')
        elif provider == 'manual':
            qs = qs.exclude(oauth_provider__iexact='google')

        return Response({
            'users': [
                {
                    'id': p.user_id,
                    'username': p.user.username,
                    'email': p.user.email,
                    'role': p.role,
                    'isProjectLead': p.isProjectLead,
                    'is_active': p.user.is_active,
                }
                for p in qs.order_by('-user__id')
            ]
        })


class ToggleProjectLeadView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, user_id):
        user = User.objects.filter(id=user_id).first()
        if user is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        profile, _ = Profile.objects.get_or_create(user=user)
        profile.isProjectLead = not profile.isProjectLead
        profile.role = 'project_lead' if profile.isProjectLead else 'developer'
        profile.save(update_fields=['isProjectLead', 'role'])
        return Response({'status': 'ok', 'id': user.id, 'isProjectLead': profile.isProjectLead})


class PipelineSettingsView(APIView):
    """GET is open to any authenticated user (needed to render a department's
    board); PATCH is admin-only. Mirrors core.views.api_pipeline_settings.
    """

    def get(self, request):
        dept = request.query_params.get('department')
        if not dept or dept not in _DEPARTMENT_CHOICES:
            return Response({'error': 'Invalid department'}, status=status.HTTP_400_BAD_REQUEST)
        mapping = _pipeline_map(dept)
        return Response({
            'department': dept,
            'stages': [{'key': k, 'label': label, 'is_visible': mapping[k]} for k, label in _PIPELINE_STAGES],
        })

    def patch(self, request):
        if not IsAdminUser().has_permission(request, self):
            return Response({'error': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)

        dept = request.data.get('department')
        visibility = request.data.get('stages')
        if not dept or dept not in _DEPARTMENT_CHOICES:
            return Response({'error': 'Invalid department'}, status=status.HTTP_400_BAD_REQUEST)
        if not isinstance(visibility, dict):
            return Response({'error': 'stages must be an object'}, status=status.HTTP_400_BAD_REQUEST)

        valid_keys = {k for k, _ in _PIPELINE_STAGES}
        for stage_key, is_visible in visibility.items():
            if stage_key not in valid_keys:
                continue
            DepartmentPipelineSettings.objects.update_or_create(
                department=dept, stage_key=stage_key,
                defaults={'is_visible': bool(is_visible), 'updated_by': request.user},
            )
        return Response({'status': 'ok', 'department': dept})


def _department_json(dept: ITDepartment) -> dict:
    return {'id': dept.pk, 'name': dept.name, 'description': dept.description}


class ITDepartmentListCreateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        depts = ITDepartment.objects.order_by('name')
        return Response({'departments': [_department_json(d) for d in depts]})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if ITDepartment.objects.filter(name__iexact=name).exists():
            return Response({'error': 'A department with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)
        dept = ITDepartment.objects.create(name=name, description=(request.data.get('description') or '').strip())
        return Response(_department_json(dept), status=status.HTTP_201_CREATED)


class ITDepartmentDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, dept_id):
        dept = ITDepartment.objects.filter(pk=dept_id).first()
        if dept is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if ITDepartment.objects.filter(name__iexact=name).exclude(pk=dept_id).exists():
            return Response({'error': 'A department with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)
        dept.name = name
        dept.description = (request.data.get('description') or '').strip()
        dept.save()
        return Response(_department_json(dept))

    def delete(self, request, dept_id):
        dept = ITDepartment.objects.filter(pk=dept_id).first()
        if dept is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        dept.delete()
        return Response({'status': 'deleted', 'id': dept_id})


def _team_json(team: ITTeam) -> dict:
    return {
        'id': team.pk, 'name': team.name,
        'department_id': team.department_id, 'department_name': team.department.name,
        'team_type': team.team_type, 'team_type_label': team.get_team_type_display(),
        'color': team.color,
    }


class ITTeamListCreateView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        teams = ITTeam.objects.select_related('department').order_by('department__name', 'name')
        return Response({'teams': [_team_json(t) for t in teams]})

    def post(self, request):
        name = (request.data.get('name') or '').strip()
        dept_id = request.data.get('department_id')
        team_type = request.data.get('team_type', 'development')
        color = (request.data.get('color') or '#4F46E5').strip()

        if not name:
            return Response({'error': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not dept_id:
            return Response({'error': 'Department is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if ITTeam.objects.filter(name__iexact=name).exists():
            return Response({'error': 'A team with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)
        dept = ITDepartment.objects.filter(pk=dept_id).first()
        if dept is None:
            return Response({'error': 'Department not found'}, status=status.HTTP_404_NOT_FOUND)

        team = ITTeam.objects.create(name=name, department=dept, team_type=team_type, color=color)
        return Response(_team_json(team), status=status.HTTP_201_CREATED)


class ITTeamDetailView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, team_id):
        team = ITTeam.objects.filter(pk=team_id).first()
        if team is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        name = (request.data.get('name') or '').strip()
        if not name:
            return Response({'error': 'Name is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if ITTeam.objects.filter(name__iexact=name).exclude(pk=team_id).exists():
            return Response({'error': 'A team with this name already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        dept_id = request.data.get('department_id', team.department_id)
        dept = ITDepartment.objects.filter(pk=dept_id).first()
        if dept is None:
            return Response({'error': 'Department not found'}, status=status.HTTP_404_NOT_FOUND)

        team.name = name
        team.department = dept
        team.team_type = request.data.get('team_type', team.team_type)
        team.color = (request.data.get('color') or team.color).strip()
        team.save()
        return Response(_team_json(team))

    def delete(self, request, team_id):
        team = ITTeam.objects.filter(pk=team_id).first()
        if team is None:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        team.delete()
        return Response({'status': 'deleted', 'id': team_id})


class StageSequenceView(APIView):
    """GET ?project_id=<id>|global, POST (admin-only) upserts the sequence.
    Mirrors core.views.api_stage_sequence.
    """

    def get(self, request):
        project_id = request.query_params.get('project_id')
        if project_id and project_id != 'global':
            qs = ProjectStageConfiguration.objects.filter(project_id=project_id)
        else:
            qs = ProjectStageConfiguration.objects.filter(project=None)

        stages = list(
            qs.select_related('team__department').order_by('seq_order').values(
                'id', 'seq_order', 'label_override',
                'team_id', 'team__name', 'team__color', 'team__team_type',
                'team__department__name',
            )
        )
        return Response({'stages': stages})

    def post(self, request):
        if not IsAdminUser().has_permission(request, self):
            return Response({'error': 'Admin access required.'}, status=status.HTTP_403_FORBIDDEN)

        raw_project = request.data.get('project_id')
        project_obj = None
        if raw_project and raw_project != 'global':
            from core.models import Project
            project_obj = Project.objects.filter(pk=raw_project).first()
            if project_obj is None:
                return Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)

        stages_data = request.data.get('stages', [])
        if not isinstance(stages_data, list):
            return Response({'error': 'stages must be an array'}, status=status.HTTP_400_BAD_REQUEST)

        ProjectStageConfiguration.objects.filter(project=project_obj).delete()
        for item in stages_data:
            team_id = item.get('team_id')
            if not team_id:
                continue
            team = ITTeam.objects.filter(pk=team_id).first()
            if team is None:
                continue
            ProjectStageConfiguration.objects.create(
                project=project_obj,
                team=team,
                seq_order=item.get('seq_order', 0),
                label_override=(item.get('label_override') or '').strip(),
            )
        return Response({'status': 'ok', 'project_id': project_obj.pk if project_obj else None})
