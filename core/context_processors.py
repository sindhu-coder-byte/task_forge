from datetime import date
from django.db.models import Q
from .models import Profile, Task

def global_user_context(request):
    if not request.user.is_authenticated:
        return {}

    # ✅ Safe profile access
    profile, _ = Profile.objects.get_or_create(user=request.user)
    role = profile.role

    # ✅ Notification logic (aligned with views.py)
    if role == 'developer':
        notification_count = Task.objects.filter(assigned_to=request.user, status='todo').count()
    elif role == 'tester':
        notification_count = Task.objects.filter(assigned_to=request.user, status='in_review').count()
    elif role in ['project_lead', 'team_lead']:
        notification_count = Task.objects.filter(
            Q(project__members=request.user) | Q(project__project_lead=request.user)
        ).count()
    else:
        notification_count = 0

    # ✅ Delayed tasks
    if role == 'admin':
        delayed = Task.objects.filter(
            due_date__lt=date.today()
        ).exclude(status='done').count()
    else:
        delayed = Task.objects.filter(
            due_date__lt=date.today(),
            assigned_to=request.user
        ).exclude(status='done').count()

    return {
        'role': role,
        'notification_count': notification_count,
        'delayed_tasks_count': delayed,
    }
    
    

def invite_roles(request):
    return {
        'invite_role_choices': Profile.ROLE_CHOICES
    }