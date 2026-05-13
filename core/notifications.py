"""
Notification Service - Automated email notifications for TaskForge
"""
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.utils import timezone
from datetime import timedelta
from django.conf import settings

from .models import Task, ProjectMembership, Project, Notification


class NotificationService:
    """Service for sending automated email notifications"""
    
    def __init__(self):
        host_user = getattr(settings, 'EMAIL_HOST_USER', None)
        default_from = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        self.from_email = host_user or default_from or 'noreply@taskforge.com'
        # Allow non-SMTP backends (console, file, locmem) used in dev/testing
        backend = getattr(settings, 'EMAIL_BACKEND', '')
        non_smtp = any(k in backend for k in ('console', 'file', 'locmem', 'memory'))
        self._email_configured = bool(host_user) or non_smtp

    def send_email(self, recipient, subject, html_content, text_content=None):
        """Send HTML email to recipient.

        Returns True on success, False on failure.
        Skips silently if EMAIL_HOST_USER is not configured (avoids SMTP auth errors).
        """
        if not self._email_configured:
            print(f"[TaskForge] EMAIL_HOST_USER not set — skipping email to {recipient.email}")
            return False

        if not recipient.email:
            return False

        if text_content is None:
            text_content = strip_tags(html_content) if html_content else subject

        try:
            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=self.from_email,
                to=[recipient.email],
            )
            if html_content:
                email.attach_alternative(html_content, "text/html")
            email.send(fail_silently=False)
            return True
        except Exception as e:
            print(f"[TaskForge] Email to {recipient.email} failed: {e}")
            return False
    
    def get_project_lead(self, project):
        """Get the project lead FK field (single user or None)."""
        return project.project_lead

    def get_project_leads(self, project):
        """Return every active user who is a project lead for this project.

        Unions two sources:
          1. project.project_lead FK (set directly by admin on the Project model).
          2. ProjectMembership rows with role='project_lead' (assigned via membership).
        Both paths are checked because admins can set one without the other.
        """
        leads = set()
        if project.project_lead and project.project_lead.is_active:
            leads.add(project.project_lead)
        for m in ProjectMembership.objects.filter(
            project=project, role='project_lead'
        ).select_related('user'):
            if m.user.is_active:
                leads.add(m.user)
        return leads
    
    def get_role_holders(self, project, roles):
        """Get all users with specified roles in a project"""
        memberships = ProjectMembership.objects.filter(
            project=project,
            role__in=roles
        ).select_related('user')
        return [m.user for m in memberships]

    def get_task_role(self, task):
        """Return the assignee's role in this task's project."""
        if not task.project_id or not task.assigned_to_id:
            return None

        return ProjectMembership.objects.filter(
            project=task.project,
            user=task.assigned_to,
        ).values_list('role', flat=True).first()

    def get_admin_users(self):
        """Return all active users whose Profile.role is 'admin', plus any in TASKFORGE_ADMIN_EMAILS."""
        from .models import Profile
        from django.contrib.auth.models import User

        admins = set()

        for profile in Profile.objects.filter(role='admin').select_related('user'):
            if profile.user.is_active and profile.user.email:
                admins.add(profile.user)

        admin_emails = getattr(settings, 'TASKFORGE_ADMIN_EMAILS', [])
        if admin_emails:
            for u in User.objects.filter(email__in=admin_emails, is_active=True):
                admins.add(u)

        return admins

    def get_relevant_role_holders(self, task, trigger=None, status=None):
        """Resolve project role recipients for workflow and deadline events."""
        if not task.project_id:
            return []

        roles = set()
        assignee_role = self.get_task_role(task)
        if assignee_role:
            roles.add(assignee_role)

        status = status or task.status
        if trigger in {'due_soon', 'overdue'}:
            roles.update(['project_lead', 'delivery_team'])
        elif status == 'in_review':
            roles.update(['tester', 'qa'])
        elif status == 'in_progress':
            roles.update(['developer', 'ui_ux_designer'])
        elif status == 'done':
            roles.update(['project_lead', 'delivery_team'])

        return self.get_role_holders(task.project, roles)

    def notification_exists_today(self, user, task, title):
        return Notification.objects.filter(
            user=user,
            task=task,
            title=title,
            created_at__date=timezone.now().date(),
        ).exists()
    
    def get_task_url(self, task):
        """Generate task URL"""
        base_url = getattr(settings, 'SITE_URL', '').rstrip('/')
        path = f"/tasks/{task.id}/"
        return f"{base_url}{path}" if base_url else path

    def get_project_url(self, project):
        base_url = getattr(settings, 'SITE_URL', '').rstrip('/')
        path = f"/projects/{project.id}/"
        return f"{base_url}{path}" if base_url else path

    def notify_user(self, user, notification_type, title, message, task=None, project=None, email_subject=None):
        if not user or not getattr(user, 'is_active', False):
            return False

        Notification.objects.create(
            user=user,
            notification_type=notification_type,
            title=title,
            message=message,
            task=task,
            project=project,
        )

        if user.email:
            body = message
            if task:
                body = f"{message}\n\nOpen task: {self.get_task_url(task)}"
            elif project:
                body = f"{message}\n\nOpen project: {self.get_project_url(project)}"
            self.send_email(
                recipient=user,
                subject=email_subject or title,
                html_content=body.replace("\n", "<br>"),
                text_content=body,
            )
        return True

    def notify_task_created(self, task, created_by):
        recipients = set()
        if task.assigned_to:
            recipients.add(task.assigned_to)
        if task.project:
            recipients.update(self.get_project_leads(task.project))
        if task.team and task.team.lead:
            recipients.add(task.team.lead)
        if task.project:
            recipients.update(self.get_role_holders(task.project, ['delivery_team']))

        project_leads = self.get_project_leads(task.project) if task.project else set()
        project_name  = task.project.name if task.project else "—"
        task_url      = self.get_task_url(task)

        for recipient in recipients:
            if recipient == created_by:
                continue

            is_assignee = recipient == task.assigned_to
            is_lead     = recipient in project_leads

            if is_assignee:
                recipient_role = 'assignee'
                notif_type     = 'task_assigned'
                title          = f"New task assigned to you: {task.issue_key}"
                message        = (
                    f"{created_by.get_full_name() or created_by.username} assigned "
                    f"'{task.title}' to you in {project_name}."
                )
                subject        = f"Task assigned: {task.issue_key} — {task.title}"
            elif is_lead:
                recipient_role = 'project_lead'
                notif_type     = 'task_updated'
                title          = f"New task in your project: {task.issue_key}"
                message        = (
                    f"{created_by.get_full_name() or created_by.username} created "
                    f"'{task.title}' in {project_name}. "
                    f"As project lead, please review and assign resources as needed."
                )
                subject        = f"New task created: {task.issue_key} — {task.title}"
            else:
                recipient_role = 'member'
                notif_type     = 'task_updated'
                title          = f"New task: {task.issue_key}"
                message        = (
                    f"{created_by.get_full_name() or created_by.username} created "
                    f"'{task.title}' in {project_name}."
                )
                subject        = f"New task: {task.issue_key} — {task.title}"

            Notification.objects.create(
                user=recipient,
                notification_type=notif_type,
                title=title,
                message=message,
                task=task,
                project=task.project,
            )

            if recipient.email:
                context = {
                    'recipient':      recipient,
                    'recipient_role': recipient_role,
                    'task':           task,
                    'created_by':     created_by,
                    'project_name':   project_name,
                    'task_url':       task_url,
                    'email_subject':  subject,
                }
                html_content = render_to_string('emails/task_assigned.html', context)
                self.send_email(
                    recipient=recipient,
                    subject=subject,
                    html_content=html_content,
                )

    def notify_project_member_added(self, project, user, role, added_by, team=None):
        from django.contrib.auth.models import User as AuthUser

        role_display = dict(ProjectMembership.ROLE_CHOICES).get(role, role.replace('_', ' ').title())
        project_url  = self.get_project_url(project)

        # Notify the new member (welcome email)
        team_text = f" and added you to team {team.name}" if team else ""
        welcome_message = (
            f"{added_by.get_full_name() or added_by.username} added you to "
            f"{project.name} as {role_display}{team_text}."
        )
        Notification.objects.create(
            user=user,
            notification_type='member_added',
            title=f"Added to project: {project.name}",
            message=welcome_message,
            project=project,
        )
        if user.email:
            context = {
                'recipient':         user,
                'notification_type': 'welcome',
                'project':           project,
                'role':              role,
                'role_display':      role_display,
                'added_by':          added_by,
                'team':              team,
                'project_url':       project_url,
                'email_subject':     f"You were added to {project.name}",
            }
            html_content = render_to_string('emails/member_added.html', context)
            self.send_email(
                recipient=user,
                subject=f"You've been added to {project.name} as {role_display}",
                html_content=html_content,
            )

        # Notify all project leads (except if they are the person being added or the one doing the adding)
        for lead in self.get_project_leads(project):
            if lead == added_by or lead == user:
                continue
            lead_message = (
                f"{added_by.get_full_name() or added_by.username} added "
                f"{user.get_full_name() or user.username} as {role_display}."
            )
            Notification.objects.create(
                user=lead,
                notification_type='member_added',
                title=f"New member added: {project.name}",
                message=lead_message,
                project=project,
            )
            if lead.email:
                context = {
                    'recipient':         lead,
                    'notification_type': 'lead_notice',
                    'project':           project,
                    'new_member':        user,
                    'role':              role,
                    'role_display':      role_display,
                    'added_by':          added_by,
                    'team':              team,
                    'project_url':       project_url,
                    'email_subject':     f"New member added to {project.name}",
                }
                html_content = render_to_string('emails/member_added.html', context)
                self.send_email(
                    recipient=lead,
                    subject=f"New member added to {project.name}: {user.get_full_name() or user.username}",
                    html_content=html_content,
                )

    def notify_team_changed(self, team, actor, action="updated"):
        recipients = set(team.members.all())
        if team.lead:
            recipients.add(team.lead)
        if team.project.project_lead:
            recipients.add(team.project.project_lead)

        for recipient in recipients:
            if recipient == actor:
                continue
            self.notify_user(
                user=recipient,
                notification_type='project_updated',
                title=f"Team {action}: {team.name}",
                message=f"{actor.username} {action} team {team.name} in {team.project.name}.",
                project=team.project,
                email_subject=f"Team {action}: {team.name}",
            )

    def notify_file_uploaded(self, attachment, uploaded_by):
        task = attachment.task
        recipients = set()
        if task.assigned_to:
            recipients.add(task.assigned_to)
        if task.project:
            recipients.update(self.get_project_leads(task.project))
        if task.team and task.team.lead:
            recipients.add(task.team.lead)

        filename = attachment.file.name.split('/')[-1] if attachment.file else "file"
        task_url  = self.get_task_url(task)
        uploader_name = uploaded_by.get_full_name() or uploaded_by.username

        for recipient in recipients:
            if recipient == uploaded_by:
                continue

            title   = f"File added: {task.issue_key}"
            message = f"{uploader_name} uploaded '{filename}' to '{task.title}'."

            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=title,
                message=message,
                task=task,
                project=task.project,
            )

            if recipient.email:
                context = {
                    'recipient':    recipient,
                    'task':         task,
                    'uploaded_by':  uploaded_by,
                    'filename':     filename,
                    'task_url':     task_url,
                }
                html_content = render_to_string('emails/file_uploaded.html', context)
                self.send_email(
                    recipient=recipient,
                    subject=f"File uploaded to {task.issue_key}: {filename}",
                    html_content=html_content,
                )
    
    # ============================================================
    # NOTIFICATION TRIGGERS
    # ============================================================
    
    def notify_due_date_approaching(self, task, days_before=1):
        """Send due-date reminder with role-targeted recipients.

        Recipient rules:
          - All windows : notify the assignee + every project lead
            (FK and ProjectMembership-based).
          - days_before <= 1: also notify the team lead (1-day urgency escalation).
        """
        if not task.due_date or not task.project:
            return

        recipients = set()

        # Always notify the assignee
        if task.assigned_to:
            recipients.add(task.assigned_to)

        # Always notify all project leads (FK + membership-based)
        recipients.update(self.get_project_leads(task.project))

        # Escalate to team lead when only 1 day remains
        if days_before <= 1:
            if task.team and task.team.lead:
                recipients.add(task.team.lead)

        task_url = self.get_task_url(task)

        for recipient in recipients:
            if not recipient.email:
                continue

            title = f"Task Due Soon: {task.issue_key}"
            if self.notification_exists_today(recipient, task, title):
                continue

            # Resolve the recipient's role in this project for role-specific messaging
            recipient_role = ProjectMembership.objects.filter(
                user=recipient, project=task.project,
            ).values_list('role', flat=True).first()
            if not recipient_role:
                profile = getattr(recipient, 'profile', None)
                recipient_role = getattr(profile, 'role', 'user') or 'user'

            context = {
                'recipient':      recipient,
                'task':           task,
                'task_url':       task_url,
                'days_before':    days_before,
                'recipient_role': recipient_role,
            }

            html_content = render_to_string('emails/due_date_reminder.html', context)

            if days_before == 0:
                subject = f"[Due Today] {task.issue_key} – {task.title}"
            else:
                subject = f"[{days_before}d left] {task.issue_key} – {task.title}"
            self.send_email(
                recipient=recipient,
                subject=subject,
                html_content=html_content,
            )

            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=title,
                message=f"Task '{task.title}' is due in {days_before} day(s) on {task.due_date}.",
                task=task,
                project=task.project,
            )
    
    def notify_status_change(self, task, old_status, new_status, updated_by):
        """Send notification for status changes (e.g., Developer to Tester handoff)"""
        if not task.project:
            return
        
        recipients = set()

        # All project leads (FK + membership-based)
        recipients.update(self.get_project_leads(task.project))

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

        recipients.update(self.get_relevant_role_holders(
            task,
            trigger='status_change',
            status=new_status,
        ))

        # Add team members for task transitions
        if task.team:
            recipients.update(task.team.members.all())
            if task.team.lead:
                recipients.add(task.team.lead)
        
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
                'old_status_label': dict(Task.STATUS_CHOICES).get(old_status, old_status),
                'new_status_label': dict(Task.STATUS_CHOICES).get(new_status, new_status),
                'updated_by': updated_by,
                'task_url': task_url,
            }
            
            html_content = render_to_string('emails/status_change.html', context)
            
            self.send_email(
                recipient=recipient,
                subject=f"Task Status Changed: {task.issue_key} - {dict(Task.STATUS_CHOICES).get(new_status, new_status)}",
                html_content=html_content
            )
            
            # Create in-app notification
            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=f"Task Status Changed: {task.issue_key}",
                message=(
                    f"Task '{task.title}' status changed from "
                    f"{dict(Task.STATUS_CHOICES).get(old_status, old_status)} to "
                    f"{dict(Task.STATUS_CHOICES).get(new_status, new_status)}"
                ),
                task=task,
                project=task.project,
            )
    
    def notify_overdue_task(self, task):
        """Send overdue alert to the assignee, all project leads, and all admins.

        Recipient rules:
          - Always notify the assignee.
          - Always notify every project lead (FK and ProjectMembership-based).
          - Always notify every user with Profile.role='admin' (or listed in TASKFORGE_ADMIN_EMAILS).
        """
        if not task.due_date or not task.project:
            return

        days_overdue = (timezone.now().date() - task.due_date).days

        recipients = set()

        if task.assigned_to:
            recipients.add(task.assigned_to)

        recipients.update(self.get_project_leads(task.project))
        recipients.update(self.get_admin_users())

        task_url = self.get_task_url(task)

        for recipient in recipients:
            if not recipient.email:
                continue

            title = f"Overdue Task: {task.issue_key}"
            if self.notification_exists_today(recipient, task, title):
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
                subject=f"⚠️ Overdue {days_overdue}d: {task.issue_key} – {task.title}",
                html_content=html_content,
            )

            Notification.objects.create(
                user=recipient,
                notification_type='task_updated',
                title=title,
                message=f"Task '{task.title}' is {days_overdue} day(s) overdue.",
                task=task,
                project=task.project,
            )

    def send_project_progress_digest(self, project):
        """Send a project-wide progress digest to every project lead.

        Covers: overall completion %, per-status counts, tasks due in 7 days,
        and overdue tasks. Deduplicates — at most one digest per project+lead per day.
        """
        today = timezone.now().date()
        tasks_qs = Task.objects.filter(project=project).select_related('assigned_to')

        status_counts = {
            s: tasks_qs.filter(status=s).count()
            for s in ('todo', 'in_progress', 'in_review', 'qa', 'done')
        }
        total_tasks   = sum(status_counts.values())
        overdue_count = tasks_qs.filter(due_date__lt=today).exclude(status='done').count()
        completed     = status_counts['done'] + status_counts['in_review']
        progress_pct  = int(completed / total_tasks * 100) if total_tasks else 0

        due_soon = list(
            tasks_qs.filter(
                due_date__gte=today,
                due_date__lte=today + timedelta(days=7),
            ).exclude(status='done').order_by('due_date')[:10]
        )
        overdue = list(
            tasks_qs.filter(due_date__lt=today)
            .exclude(status='done').order_by('due_date')[:15]
        )

        base_url = getattr(settings, 'SITE_URL', '').rstrip('/')
        for t in due_soon:
            t._email_url = f"{base_url}/tasks/{t.id}/"
        for t in overdue:
            t._email_url   = f"{base_url}/tasks/{t.id}/"
            t._days_overdue = (today - t.due_date).days

        digest_title = f"Project Progress: {project.name}"
        project_url  = self.get_project_url(project)

        def _sent_today(user):
            return Notification.objects.filter(
                user=user,
                project=project,
                title__startswith="Project Progress:",
                created_at__date=today,
            ).exists()

        for lead in self.get_project_leads(project):
            if not lead.email or _sent_today(lead):
                continue

            context = {
                'project':        project,
                'recipient':      lead,
                'stats': {
                    'total':            total_tasks,
                    'todo':             status_counts['todo'],
                    'in_progress':      status_counts['in_progress'],
                    'in_review':        status_counts['in_review'],
                    'qa':               status_counts['qa'],
                    'done':             status_counts['done'],
                    'overdue_count':    overdue_count,
                    'progress_percent': progress_pct,
                },
                'due_soon_tasks': due_soon,
                'overdue_tasks':  overdue,
                'project_url':    project_url,
                'today_date':     today,
            }
            html_content = render_to_string('emails/project_progress_digest.html', context)

            self.send_email(
                recipient=lead,
                subject=f"[{today}] Project Progress: {project.name} — {progress_pct}% complete",
                html_content=html_content,
            )

            Notification.objects.create(
                user=lead,
                project=project,
                notification_type='project_updated',
                title=digest_title,
                message=(
                    f"{project.name}: {total_tasks} task(s), "
                    f"{progress_pct}% complete, {overdue_count} overdue."
                ),
            )


# ============================================================
# SCHEDULED TASKS (to be called by cron/celery)
# ============================================================

def check_due_dates(days_before=1):
    """Send approaching due-date reminders for tasks due in exactly `days_before` days."""
    from django.utils import timezone

    service = NotificationService()
    today = timezone.now().date()
    target_date = today + timedelta(days=days_before)

    tasks_due_soon = Task.objects.filter(
        due_date=target_date,
        status__in=['todo', 'in_progress', 'in_review', 'qa'],
    ).select_related('project', 'assigned_to')

    checked = 0
    for task in tasks_due_soon:
        service.notify_due_date_approaching(task, days_before=days_before)
        checked += 1
    return checked


def check_due_today():
    """Send 'due today' alerts for every open task whose due date is today.

    Covers all non-done statuses including QA.
    Uses days_before=0 so the template renders the red 'Due Today' banner.
    """
    from django.utils import timezone

    service = NotificationService()
    today = timezone.now().date()

    tasks_due_today = Task.objects.filter(
        due_date=today,
        status__in=['todo', 'in_progress', 'in_review', 'qa'],
    ).select_related('project', 'assigned_to')

    checked = 0
    for task in tasks_due_today:
        service.notify_due_date_approaching(task, days_before=0)
        checked += 1
    return checked


def check_overdue_tasks():
    """Check for overdue tasks and send notifications"""
    from django.utils import timezone
    
    service = NotificationService()
    today = timezone.now().date()
    
    # Find overdue tasks
    overdue_tasks = Task.objects.filter(
        due_date__lt=today,
        status__in=['todo', 'in_progress', 'in_review', 'qa'],
    ).select_related('project', 'assigned_to')

    checked = 0
    for task in overdue_tasks:
        service.notify_overdue_task(task)
        checked += 1
    return checked


def send_project_digests():
    """Send a progress digest email to all project leads for every project
    that has at least one active (non-done) task."""
    service = NotificationService()
    active_ids = (
        Task.objects.exclude(status='done')
        .values_list('project_id', flat=True)
        .distinct()
    )
    projects = Project.objects.filter(id__in=active_ids).select_related('project_lead')
    count = 0
    for project in projects:
        service.send_project_progress_digest(project)
        count += 1
    return count
