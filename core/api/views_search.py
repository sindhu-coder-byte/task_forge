from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import Project, Task


class SearchView(APIView):
    """Mirrors core.views.search_view's role-scoped combined task/project search."""

    def get(self, request):
        query = (request.query_params.get('q') or '').strip()
        role = getattr(request.user.profile, 'role', 'user')
        results = []

        if not query:
            return Response({'results': results})

        if role == 'admin':
            base_tasks = Task.objects.all()
            base_projects = Project.objects.all()
        elif role == 'project_lead':
            from django.db.models import Q
            base_tasks = Task.objects.filter(
                Q(project__project_lead=request.user) | Q(assigned_to=request.user)
            )
            base_projects = Project.objects.filter(project_lead=request.user)
        else:
            base_tasks = Task.objects.filter(assigned_to=request.user)
            base_projects = Project.objects.filter(members=request.user)

        combined_ids = set()
        combined_results = []

        def add_task(task):
            if task.id in combined_ids:
                return
            combined_ids.add(task.id)
            combined_results.append({
                'id': task.id,
                'title': task.title,
                'issue_key': task.issue_key,
                'project_name': task.project.name if task.project_id else None,
                'status': task.status,
                'type': 'task',
            })

        if query.isdigit():
            for task in base_tasks.filter(id=int(query)):
                add_task(task)

        for task in base_tasks.select_related('project')[:200]:
            if query.lower() in task.issue_key.lower():
                add_task(task)

        for task in base_tasks.filter(title__icontains=query):
            add_task(task)

        for task in base_tasks.filter(project__name__icontains=query):
            add_task(task)

        for project in base_projects.filter(name__icontains=query)[:5]:
            combined_results.append({
                'id': project.id,
                'title': project.name,
                'issue_key': None,
                'project_name': project.name,
                'status': None,
                'type': 'project',
            })

        results = combined_results[:10]
        return Response({'results': results})
