from datetime import date
from django.db.models import Q
from .models import Profile, Task
from .utils import get_notification_count

def global_user_context(request):
    if not request.user.is_authenticated:
        return {}

    profile, _ = Profile.objects.get_or_create(user=request.user)

    # ✅ use utils instead of duplicating logic
    notification_count = get_notification_count(request.user)

    # delayed tasks (keep here — different logic)
    if profile.role == 'admin':
        delayed = Task.objects.filter(
            due_date__lt=date.today()
        ).exclude(status='done').count()
    else:
        delayed = Task.objects.filter(
            due_date__lt=date.today(),
            assigned_to=request.user
        ).exclude(status='done').count()

    return {
        'role': profile.role,
        'notification_count': notification_count,
        'delayed_tasks_count': delayed,
    }
    

def invite_roles(request):
    return {
        'invite_role_choices': Profile.ROLE_CHOICES
    }
    
