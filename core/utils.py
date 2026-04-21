# utils.py (NEW FILE 🔥)
from django.db.models import Q, Count
from .models import Task

def get_user_role(user):
    return getattr(user, 'profile', None).role if hasattr(user, 'profile') else 'user'


def get_notification_count(user):
    profile = getattr(user, 'profile', None)
    if not profile:
        return 0
    role = profile.role

    if role == 'developer':
        return Task.objects.filter(assigned_to=user, status='todo').count()
    elif role == 'tester':
        return Task.objects.filter(assigned_to=user, status='in_review').count()
    elif role in ['project_lead', 'team_lead']:
        return Task.objects.filter(
            Q(project__members=user) | Q(project__project_lead=user)
        ).count()
    return 0


def base_task_queryset():
    return Task.objects.select_related(
        'project', 'assigned_to', 'created_by'
    ).prefetch_related('labels')
    
def get_status_counts(tasks):
    return {
        'todo': tasks.filter(status='todo').count(),
        'in_progress': tasks.filter(status='in_progress').count(),
        'in_review': tasks.filter(status='in_review').count(),
        'done': tasks.filter(status='done').count(),
    }