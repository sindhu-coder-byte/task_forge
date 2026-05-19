from datetime import date
from .models import Profile, ProjectMembership, Task, Notification
from django.conf import settings


def _is_admin_user(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    email = (getattr(user, "email", "") or "").strip().lower()
    if email and email in getattr(settings, "TASKFORGE_ADMIN_EMAILS", []):
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == "admin")

def global_user_context(request):
    if not request.user.is_authenticated:
        return {}

    # Ensure profile exists for template safety (and for superusers).
    profile, _ = Profile.objects.get_or_create(user=request.user)

    # If superuser (or allowlisted email), treat as admin in UI.
    if _is_admin_user(request.user) and profile.role != "admin":
        profile.role = "admin"
        profile.save(update_fields=["role"])
    
    if not profile:
     return {
        'profile_role': None,
        'is_project_lead': False,
        'role': None,
        'notification_count': 0,
        'delayed_tasks_count': 0,
     }
    profile_role = profile.role if profile else None
    is_project_lead = bool(
        profile and (
            profile.role == 'project_lead' or
            profile.isProjectLead or
            ProjectMembership.objects.filter(user=request.user, role='project_lead').exists()
        )
    )

    # Profile.role is the authoritative global role. Never let a project-specific
    # membership role (which can be stale, e.g. 'admin') shadow it here.
    role = profile_role

    notification_count = Notification.objects.filter(user=request.user, is_read=False).count()

    # ✅ Role-based delayed tasks
    if profile_role == 'admin' or is_project_lead or role == 'project_lead':
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
        'profile_role': profile_role,
        'is_project_lead': is_project_lead,
        'notification_count': notification_count,
        'delayed_tasks_count': delayed,
    }
    

def invite_roles(request):
    return {
        'invite_role_choices': ProjectMembership.ROLE_CHOICES
    }
    
