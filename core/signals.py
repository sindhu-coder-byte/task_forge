from allauth.socialaccount.signals import pre_social_login
from django.dispatch import receiver
from django.contrib.auth.models import User
from core.models import Profile

@receiver(pre_social_login)
def handle_google_login(request, sociallogin, **kwargs):
    email = sociallogin.account.extra_data.get('email')

    if not email:
        return

    user = User.objects.filter(email__iexact=email).first()

    # ✅ If user already exists → normal flow
    if user:
        sociallogin.connect(request, user)
        return

    # ✅ NEW: Allow random Google login → create user
    username = email.split("@")[0]

    user = User.objects.create_user(
        username=username,
        email=email
    )
    user.set_unusable_password()
    user.save()

    # 🔒 Assign SAFE default role
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.role = "guest"   # VERY IMPORTANT
    profile.save()

    sociallogin.connect(request, user)


# ============================================================
# TASK NOTIFICATION SIGNALS
# ============================================================

from django.db.models.signals import post_save, pre_save
from django.contrib.auth.models import User
from core.models import Task, TaskActivity
from core.notifications import NotificationService

notification_service = NotificationService()


@receiver(pre_save, sender=Task)
def task_status_change_handler(sender, instance, **kwargs):
    """Detect status changes and trigger notifications"""
    if not instance.pk:
        # New task - skip
        return
    
    try:
        old_task = Task.objects.get(pk=instance.pk)
        old_status = old_task.status
        new_status = instance.status
        
        # Store old values for post_save to use
        instance._old_status = old_status
        instance._old_assigned_to = old_task.assigned_to
    except Task.DoesNotExist:
        pass


@receiver(post_save, sender=Task)
def task_after_save_handler(sender, instance, created, **kwargs):
    """Handle task save events and send notifications"""
    # Check for status change
    if hasattr(instance, '_old_status') and instance._old_status != instance.status:
        # Get the user who made the change (from request if available)
        # For now, we'll use created_by as the updater
        updated_by = instance.created_by
        
        notification_service.notify_status_change(
            task=instance,
            old_status=instance._old_status,
            new_status=instance.get_status_display(),
            updated_by=updated_by
        )
    
    # Log activity
    if created:
        TaskActivity.objects.create(
            task=instance,
            user=instance.created_by,
            action=f"Task created: {instance.title}"
        )