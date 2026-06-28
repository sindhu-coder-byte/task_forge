# ============================================================
# TASK NOTIFICATION SIGNALS
# ============================================================

from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
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
        instance._old_due_date = old_task.due_date
    except Task.DoesNotExist:
        pass


@receiver(post_save, sender=Task)
def task_after_save_handler(sender, instance, created, **kwargs):
    """Handle task save events and send notifications"""
    # Check for status change
    if hasattr(instance, '_old_status') and instance._old_status != instance.status:
        updated_by = getattr(instance, '_updated_by', None) or instance.created_by
        
        notification_service.notify_status_change(
            task=instance,
            old_status=instance._old_status,
            new_status=instance.status,
            updated_by=updated_by
        )
    
    # Log activity
    if created:
        TaskActivity.objects.create(
            task=instance,
            user=instance.created_by,
            action=f"Task created: {instance.title}"
        )
