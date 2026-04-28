"""
Notification Service - Automated email notifications for TaskForge
"""
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

from .models import Task, ProjectMembership, Project, Notification


class NotificationService:
    """Service for sending automated email notifications"""
    
    def __init__(self):
        self.from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', 'noreply@taskforge.com')
    
    def send_email(self, recipient, subject, html_content, text_content=None):
        """Send email to recipient"""
        if text_content is None:
            text_content = subject
        
        try:
            send_mail(
                subject=subject,
                message=text_content,
                from_email=self.from_email,
                recipient_list=[recipient.email],
                html_message=html_content,
                fail_silently=False,
            )
            return True
        except Exception as e:
            print(f"Error sending email to {recipient.email}: {e}")
            return False
    
    def get_project_lead(self, project):
        """Get the project lead for a project"""
        return project.project_lead
    
    def get_role_holders(self, project, roles):
        """Get all users with specified roles in a project"""
        memberships = ProjectMembership.objects.filter(
            project=project,
            role__in=roles
        ).select_related('user')
        return [m.user for m in memberships]
    
    def get_task_url(self, task):
        """Generate task URL"""
        return f"/core/task/{task.id}/"
    
    # ============================================================
    # NOTIFICATION TRIGGERS
    # ============================================================
    
    def notify_due_date_approaching(self, task, days_before=1):
        """Send notification for approaching due date"""
        if not task.due_date or not task.project:
            return
        
        # Get project lead and relevant role holders
        project_lead = self.get_project_lead(task.project)
        recipients = set()
        
        if project_lead:
            recipients.add(project_lead)
        
        # Add testers and QA for notification
        recipients.update(self.get_role_holders(task.project, ['tester', 'qa']))
        
        # Add the assigned user
        if task.assigned_to:
            recipients.add(task.assigned_to)
        
        task_url = self.get_task_url(task)
        
        for recipient in recipients:
            if not recipient.email:
                continue
            
            context = {
                'recipient': recipient,
                'task': task,
                'task_url': task_url,
            }
            
            html_content = render_to_string('emails/due_date_reminder.html', context)
            
            self.send_email(
                recipient=recipient,
                subject=f"Task Due Soon: {task.issue_key} - {task.title}",
                html_content=html_content
            )
            
            # Create in-app notification
            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=f"Task Due Soon: {task.issue_key}",
                message=f"Task '{task.title}' is due on {task.due_date}",
                task=task,
                project=task.project,
            )
    
    def notify_status_change(self, task, old_status, new_status, updated_by):
        """Send notification for status changes (e.g., Developer to Tester handoff)"""
        if not task.project:
            return
        
        recipients = set()
        
        # Get project lead
        project_lead = self.get_project_lead(task.project)
        if project_lead:
            recipients.add(project_lead)
        
        # Determine target roles based on new status
        if new_status == 'in_review':
            # Notify testers and QA for handoff
            recipients.update(self.get_role_holders(task.project, ['tester', 'qa']))
        elif new_status == 'in_progress':
            # Notify developers
            recipients.update(self.get_role_holders(task.project, ['developer']))
        elif new_status == 'done':
            # Notify reporter and project lead
            if task.reporter:
                recipients.add(task.reporter)
        
        # Add assigned user if different from updater
        if task.assigned_to and task.assigned_to != updated_by:
            recipients.add(task.assigned_to)
        
        task_url = self.get_task_url(task)
        
        for recipient in recipients:
            if not recipient.email or recipient == updated_by:
                continue
            
            context = {
                'recipient': recipient,
                'task': task,
                'old_status': old_status,
                'new_status': new_status,
                'updated_by': updated_by,
                'task_url': task_url,
            }
            
            html_content = render_to_string('emails/status_change.html', context)
            
            self.send_email(
                recipient=recipient,
                subject=f"Task Status Changed: {task.issue_key} - {new_status}",
                html_content=html_content
            )
            
            # Create in-app notification
            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=f"Task Status Changed: {task.issue_key}",
                message=f"Task '{task.title}' status changed from {old_status} to {new_status}",
                task=task,
                project=task.project,
            )
    
    def notify_overdue_task(self, task):
        """Send notification for missed deadlines"""
        if not task.due_date or not task.project:
            return
        
        # Calculate days overdue
        days_overdue = (timezone.now().date() - task.due_date).days
        
        recipients = set()
        
        # Get project lead
        project_lead = self.get_project_lead(task.project)
        if project_lead:
            recipients.add(project_lead)
        
        # Add assigned user
        if task.assigned_to:
            recipients.add(task.assigned_to)
        
        # Add delivery team
        recipients.update(self.get_role_holders(task.project, ['delivery_team']))
        
        task_url = self.get_task_url(task)
        
        for recipient in recipients:
            if not recipient.email:
                continue
            
            context = {
                'recipient': recipient,
                'task': task,
                'days_overdue': days_overdue,
                'task_url': task_url,
            }
            
            html_content = render_to_string('emails/overdue_task.html', context)
            
            self.send_email(
                recipient=recipient,
                subject=f"⚠️ Overdue Task: {task.issue_key} - {task.title}",
                html_content=html_content
            )
            
            # Create in-app notification
            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=f"Overdue Task: {task.issue_key}",
                message=f"Task '{task.title}' is {days_overdue} days overdue",
                task=task,
                project=task.project,
            )


# ============================================================
# SCHEDULED TASKS (to be called by cron/celery)
# ============================================================

def check_due_dates():
    """Check for tasks due today and send reminders"""
    from django.utils import timezone
    from datetime import timedelta
    
    service = NotificationService()
    today = timezone.now().date()
    tomorrow = today + timedelta(days=1)
    
    # Find tasks due tomorrow
    tasks_due_tomorrow = Task.objects.filter(
        due_date=tomorrow,
        status__in=['todo', 'in_progress']
    ).select_related('project', 'assigned_to')
    
    for task in tasks_due_tomorrow:
        service.notify_due_date_approaching(task, days_before=1)


def check_overdue_tasks():
    """Check for overdue tasks and send notifications"""
    from django.utils import timezone
    
    service = NotificationService()
    today = timezone.now().date()
    
    # Find overdue tasks
    overdue_tasks = Task.objects.filter(
        due_date__lt=today,
        status__in=['todo', 'in_progress', 'in_review']
    ).select_related('project', 'assigned_to')
    
    for task in overdue_tasks:
        service.notify_overdue_task(task)