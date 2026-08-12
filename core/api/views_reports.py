from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Project, Task


class ReportsView(APIView):
    """Mirrors core.views.reports_view's role-scoped task/project aggregation
    (minus CSV export, which doesn't apply to a native client).
    """

    def get(self, request):
        role = request.user.profile.role
        project_id = request.query_params.get('project')
        status_filter = request.query_params.get('status')
        user_id = request.query_params.get('user')

        if role == 'admin':
            tasks = Task.objects.select_related('project', 'assigned_to').all()
            projects = Project.objects.all()
        else:
            tasks = Task.objects.select_related('project', 'assigned_to').filter(
                Q(project__members=request.user) | Q(assigned_to=request.user)
            ).distinct()
            projects = Project.objects.filter(members=request.user)

        if project_id:
            tasks = tasks.filter(project_id=project_id)
        if status_filter:
            tasks = tasks.filter(status=status_filter)
        if user_id and role == 'admin':
            tasks = tasks.filter(assigned_to_id=user_id)

        status_summary = {
            key: tasks.filter(status=key).count()
            for key, _ in Task.STATUS_CHOICES
        }

        project_summary = []
        for p in projects:
            p_tasks = tasks.filter(project=p)
            total = p_tasks.count()
            done = p_tasks.filter(status='done').count()
            project_summary.append({
                'project_id': p.id,
                'project_name': p.name,
                'key_prefix': p.key_prefix,
                'total': total,
                'completed': done,
                'progress': int((done / total) * 100) if total else 0,
            })

        return Response({
            'status_summary': status_summary,
            'project_summary': project_summary,
        })
