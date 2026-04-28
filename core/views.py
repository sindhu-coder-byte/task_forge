from collections import Counter
from datetime import date, timedelta
import profile
from urllib import request
import secrets
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth import login
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, logout
from django.http import HttpResponseForbidden, JsonResponse
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from requests import request

from .models import Project, Task, Profile, Comment, TaskActivity, TaskAttachment, Label, ProjectInvite, Notification, Team
from .forms import UserCreateForm, UserUpdateForm

from .decorators import role_required


def _get_notification_count(user):
    """Get count of unread notifications"""
    return Notification.objects.filter(user=user, is_read=False).count()


def _create_notification(user, notification_type, title, message, task=None, project=None):
    """Helper to create a notification for a user"""
    if user and user.is_active:
        Notification.objects.create(
            user=user,
            notification_type=notification_type,
            title=title,
            message=message,
            task=task,
            project=project,
            is_read=False
        )


def _project_accessible_by(user, project: Project) -> bool:
    if not user.is_authenticated:
        return False

    profile = getattr(user, 'profile', None)
    if not profile:
        return False

    return (
        profile.role == 'admin' or
        project.project_lead == user or
        project.members.filter(id=user.id).exists()
    )


def _can_manage_project_members(user, project: Project) -> bool:
    if not user.is_authenticated:
        return False

    profile = getattr(user, 'profile', None)
    if not profile:
        return False

    return (
        profile.role == 'admin' or
        project.project_lead_id == user.id or
        project.created_by_id == user.id
    )


def _task_can_view(user, task: Task) -> bool:
    role = getattr(user.profile, "role", "user")

    # Admin → everything
    if role == 'admin':
        return True

    # Project Lead → all project tasks
    if task.project and task.project.project_lead == user:
        return True

    # Delivery Team → all project tasks
    if role == 'delivery_team' and task.project and task.project.members.filter(id=user.id).exists():
        return True

    # Team Member → ONLY assigned tasks
    return task.assigned_to_id == user.id

def _allowed_transitions(role: str, old_status: str) -> set[str]:
    """
    Return allowed next statuses for a given role and current status.

    Jira-style "Transition Map" (single source of truth).
    """
    workflow_map = {
        "admin": {
            "*": {k for (k, _) in Task.STATUS_CHOICES},
        },
        "developer": {
            "todo": {"in_progress"},
            "in_progress": {"in_review"},
        },
        "tester": {
            "in_review": {"done", "in_progress"},  # approve / reject
        },
        "user": {},
        
        "project_lead": {
             "*": {k for (k, _) in Task.STATUS_CHOICES},
        },
        "ui_ux_designer": {
            "todo": {"in_progress"},
            "in_progress": {"in_review"},
        },
        "deployment_team": {
            "in_review": {"done"},
        },
        "delivery_team": {
            "*": {k for (k, _) in Task.STATUS_CHOICES},
        },
        
    }

    role_map = workflow_map.get(role, {})
    if "*" in role_map:
        return role_map["*"]
    return role_map.get(old_status, set())


def _status_label(status: str) -> str:
    return dict(Task.STATUS_CHOICES).get(status, status.replace("_", " ").title())


def _role_label(role: str) -> str:
    return dict(Profile.ROLE_CHOICES).get(role, role.replace("_", " ").title())


def _workflow_transition_rows():
    ordered_roles = ["developer", "ui_ux_designer", "tester", "deployment_team", "delivery_team", "project_lead", "admin"]
    rows = []

    for from_status, _ in Task.STATUS_CHOICES:
        for to_status, _ in Task.STATUS_CHOICES:
            if from_status == to_status:
                continue

            allowed_roles = [
                role for role in ordered_roles
                if to_status in _allowed_transitions(role, from_status)
            ]
            if not allowed_roles:
                continue

            rows.append({
                "from_status": from_status,
                "from_label": _status_label(from_status),
                "to_status": to_status,
                "to_label": _status_label(to_status),
                "roles": allowed_roles,
                "role_labels": [_role_label(role) for role in allowed_roles],
                "policy": (
                    "Visible task and assignee move only"
                    if allowed_roles == ["developer"] or allowed_roles == ["tester"]
                    else "Leads and admins can override within visible tasks"
                ),
            })

    return rows


def _log_task_activity(task: Task, user, action: str, old_value: str = "", new_value: str = ""):
    TaskActivity.objects.create(
        task=task,
        user=user,
        action=action,
        old_value=old_value or None,
        new_value=new_value or None,
    )


def _project_progress_snapshot(project: Project) -> tuple[dict, int]:
    status_counts = get_status_counts(Task.objects.filter(project=project))
    total_tasks = sum(status_counts.values())
    progress_percent = 0
    if total_tasks > 0:
        completed = status_counts.get('done', 0) + status_counts.get('in_review', 0)
        progress_percent = int((completed / total_tasks) * 100)
    return status_counts, progress_percent


def _task_can_transition(user, task: Task, new_status: str) -> bool:
    """Team-centric visibility, assignee-restricted actions (except admin)."""
    if not _task_can_view(user, task):
        return False

    role = user.profile.role
    if role in ["admin", "project_lead", "delivery_team"]:
        return new_status in _allowed_transitions(role, task.status)

    # Only assignee can move the issue in the workflow.
    if task.assigned_to_id != user.id:
        return False

    return new_status in _allowed_transitions(role, task.status)


def _task_allowed_next_statuses(user, task: Task) -> list[str]:
    allowed = []
    for status, _ in Task.STATUS_CHOICES:
        if status != task.status and _task_can_transition(user, task, status):
            allowed.append(status)
    return allowed


def _guest_home_context():
    """Safe defaults when rendering the marketing/auth shell (same template as the app home)."""
    return {
        'activities': [],
        'project_health': [],
        'admin_projects': [],
        'recent_tasks': [],
        'total_tasks': 0,
        'total_projects': 0,
        'blockers_count': 0,
        'today_tasks': 0,
        'notification_count': 0,
        'completed_tasks': 0,
        'chart_pending': 0,
        'chart_in_progress': 0,
        'chart_testing': 0,
        'chart_done': 0,
        'tasks': Task.objects.none(),
        'testing_tasks': Task.objects.none(),
        'show_role_dashboard': False,
        'total_users': None,
    }


def _user_workspace_scope(request):
    """Role-scoped tasks/projects/activity used by Home (workspace) and Dashboard (gadgets)."""
    profile, _ = Profile.objects.get_or_create(user=request.user)
    role = profile.role

    # 🔥 Better: use your helper
    notification_count = _get_notification_count(request.user)

    if role == 'admin':
        tasks_qs = Task.objects.all().select_related('project', 'assigned_to')
        projects_qs = Project.objects.all().prefetch_related('members')
        activities = list(
            TaskActivity.objects.select_related('user').order_by('-created_at')[:10]
        )
        total_users = Profile.objects.count()
        recent_tasks = list(
            Task.objects.select_related('project').order_by('-id')[:8]
        )
        testing_tasks_qs = Task.objects.none()

    elif role == 'tester':
        tasks_qs = Task.objects.filter(
            assigned_to=request.user
        ).select_related('project', 'assigned_to')

        projects_qs = Project.objects.filter(
            members=request.user
        ).prefetch_related('members')

        activities = list(
            TaskActivity.objects.filter(user=request.user)
            .order_by('-created_at')[:5]
        )

        total_users = None
        recent_tasks = []

        testing_tasks_qs = Task.objects.filter(
            status='in_review'
        ).select_related('project', 'assigned_to')

    elif role in ['project_lead', 'delivery_team']:
        # ✅ Leads can see full project scope
        tasks_qs = Task.objects.filter(
            Q(project__members=request.user) | Q(project__project_lead=request.user)
        ).select_related('project', 'assigned_to')

        projects_qs = Project.objects.filter(
            Q(members=request.user) | Q(project_lead=request.user)
        ).distinct().prefetch_related('members')

        activities = list(
            TaskActivity.objects.filter(
                Q(task__project__members=request.user) | Q(task__project__project_lead=request.user)
            ).order_by('-created_at')[:10]
        )

        total_users = None
        recent_tasks = list(tasks_qs.order_by('-id')[:10])
        testing_tasks_qs = Task.objects.none()

    else:
        tasks_qs = Task.objects.filter(
            assigned_to=request.user
        ).select_related('project', 'assigned_to')

        projects_qs = Project.objects.filter(
            members=request.user
        ).prefetch_related('members')

        activities = list(
            TaskActivity.objects.filter(user=request.user)
            .order_by('-created_at')[:5]
        )

        total_users = None
        recent_tasks = []
        testing_tasks_qs = Task.objects.none()

    return {
        'profile': profile,
        'role': role,
        'notification_count': notification_count,
        'tasks_qs': tasks_qs,
        'projects_qs': projects_qs,
        'activities': activities,
        'total_users': total_users,
        'recent_tasks': recent_tasks,
        'testing_tasks_qs': testing_tasks_qs,
    }
def home(request):
    """Home: public landing for guests, workspace for signed-in users."""
    if not request.user.is_authenticated:
        return render(request, "core/guest_home.html", _guest_home_context())

    # Enterprise workspace: project navigation + recent work (Jira-style Home).
    s = _user_workspace_scope(request)
    workspace_projects = []
    for p in s['projects_qs']:
        tsub = Task.objects.filter(project=p)
        tc = tsub.count()
        done_c = tsub.filter(status='done').count()
        pct = int(round(100 * done_c / tc)) if tc else 0
        workspace_projects.append({
            'project': p,
            'progress': pct,
            'task_count': tc,
        })
    recent_work = list(s['tasks_qs'].order_by('-id')[:15])
    return render(request, 'core/workspace_home.html', {
        'notification_count': s['notification_count'],
        'total_tasks': s['tasks_qs'].count(),
        'total_projects': s['projects_qs'].count(),
        'workspace_projects': workspace_projects,
        'recent_work': recent_work,
    })


@login_required(login_url='core:login')
def dashboard(request):
    """Analytical dashboard: charts, tables, activity stream, and role gadgets."""
    s = _user_workspace_scope(request)
    role = s['role']
    tasks_qs = s['tasks_qs']
    projects_qs = s['projects_qs']
    activities = s['activities']

    total_tasks = tasks_qs.count()
    total_projects = projects_qs.count()
    blockers_count = tasks_qs.filter(priority='High', status='todo').count()
    today_tasks = tasks_qs.filter(due_date=date.today()).count()
    completed_tasks = tasks_qs.filter(status='done').count()

    chart_pending = tasks_qs.filter(status='todo').count()
    chart_in_progress = tasks_qs.filter(status='in_progress').count()
    chart_testing = tasks_qs.filter(status='in_review').count()
    chart_done = tasks_qs.filter(status='done').count()

    admin_projects = list(projects_qs[:8]) if role == 'admin' else []
    filter_tasks = list(tasks_qs.order_by('-id')[:25])
    assigned_preview = list(
        Task.objects.filter(assigned_to=request.user)
        .select_related('project', 'assigned_to')
        .order_by('-id')[:12]
    )

    return render(request, 'core/dashboard_analytics.html', {
        'notification_count': s['notification_count'],
        'role': role,
        'show_role_dashboard': True,
        'total_tasks': total_tasks,
        'total_projects': total_projects,
        'blockers_count': blockers_count,
        'today_tasks': today_tasks,
        'completed_tasks': completed_tasks,
        'activities': activities,
        'chart_pending': chart_pending,
        'chart_in_progress': chart_in_progress,
        'chart_testing': chart_testing,
        'chart_done': chart_done,
        'tasks': tasks_qs,
        'testing_tasks': s['testing_tasks_qs'],
        'total_users': s['total_users'],
        'recent_tasks': s['recent_tasks'],
        'admin_projects': admin_projects,
        'filter_tasks': filter_tasks,
        'assigned_preview': assigned_preview,
    })
    


def post_login_handler(request, user):
    profile, _ = Profile.objects.get_or_create(user=user)

    # ✅ Apply invite if exists
    invite = ProjectInvite.objects.filter(
        email__iexact=user.email,
        used=False
    ).first()

    if invite:
        invite.project.members.add(user)

        if profile.role == 'user':  # don't override admin/lead
            profile.role = invite.role
            profile.save()

        invite.used = True
        invite.save()

    # ✅ Role-based redirect
    if profile.role == 'admin':
        return redirect('core:dashboard')
    elif profile.role in ['project_lead', 'delivery_team']:
        return redirect('core:projects')
    else:
        return redirect('core:home')  
    
@login_required(login_url='core:login')
def teams(request):
    profile = getattr(request.user, 'profile', None)
    role = profile.role if profile else 'user'

    # For team_lead, get only teams they lead
    my_teams = None
    if role == 'delivery_team':
        my_teams = Team.objects.filter(lead=request.user).prefetch_related('members', 'project')

    if role == 'admin':
        projects = Project.objects.prefetch_related('members')
        tasks = Task.objects.select_related('project', 'assigned_to').all()
        members = User.objects.filter(is_active=True)
        can_approve = True
    elif role == 'project_lead':
        projects = Project.objects.filter(project_lead=request.user).prefetch_related('members')
        tasks = Task.objects.filter(project__project_lead=request.user).select_related('project', 'assigned_to')
        members = User.objects.filter(project_members__in=projects).distinct()
        can_approve = True
    elif role == 'delivery_team':
       teams = Team.objects.filter(lead=request.user)

       team_members = User.objects.filter(teams__in=teams).distinct()

       project_ids = teams.values_list('project_id', flat=True)

       tasks = Task.objects.filter(
        assigned_to__in=team_members,
        project_id__in=project_ids
        ).select_related('project', 'assigned_to')

       projects = Project.objects.filter(id__in=project_ids).distinct()

       members = team_members
       can_approve = True
    else:
        projects = Project.objects.filter(members=request.user).distinct().prefetch_related('members')
        tasks = Task.objects.filter(project__in=projects, assigned_to=request.user).select_related('project', 'assigned_to')
        members = User.objects.filter(id=request.user.id)
        can_approve = False

    status_counts = get_status_counts(tasks)
    project_count = projects.count()
    team_member_count = members.exclude(id=request.user.id).count() if role != 'admin' else members.count()

    context = {
        'projects': projects,
        'tasks': tasks,
        'members': members,
        'status_counts': status_counts,
        'project_count': project_count,
        'team_member_count': team_member_count,
        'role': role,
        'can_approve': can_approve,
    }
    
    # Add my_teams for team_lead
    if my_teams:
        context['my_teams'] = my_teams
    
    return render(request, 'core/teams.html', context)
    
@login_required
def update_member_role(request, project_id, user_id):
    project = get_object_or_404(Project, id=project_id)

    if request.user != project.project_lead:
        return HttpResponseForbidden("Not allowed")

    user = get_object_or_404(User, id=user_id)
    new_role = request.POST.get("role")

    if new_role in ['developer', 'tester', 'ui_ux_designer', 'deployment_team', 'delivery_team']:
        user.profile.role = new_role
        user.profile.save()

    return redirect('core:project_team', project_id=project.id)

from django.contrib import messages
from django.shortcuts import redirect, get_object_or_404
from .models import Team, Project
from django.contrib.auth.models import User

from django.db import transaction

@login_required
def create_team(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if request.user != project.project_lead:
        messages.error(request, "Not allowed")
        return redirect('core:project_detail', project.id)

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        lead_id = request.POST.get("lead")
        member_ids = request.POST.getlist("members")

        if not name:
            messages.error(request, "Team name is required")
            return redirect('core:project_detail', project.id)

        try:
            with transaction.atomic():

                team = Team.objects.create(
                    name=name,
                    project=project
                )

                # ✅ Assign Lead (only valid project user)
                if lead_id:
                    lead = User.objects.filter(id=lead_id).first()
                    if lead:
                        team.lead = lead

                # ✅ Assign Members (exclude lead)
                members = User.objects.filter(id__in=member_ids)

                if lead_id:
                    members = members.exclude(id=lead_id)

                team.members.set(members)
                team.save()

                messages.success(request, "Team created successfully")

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return redirect('core:project_detail', project.id)
@login_required
def edit_team(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    project = team.project

    if request.user != project.project_lead and request.user != team.lead:
        messages.error(request, "Not allowed")
        return redirect('core:teams')

    if request.method == "POST":
        name = request.POST.get("name", "").strip()
        lead_id = request.POST.get("lead")
        member_ids = request.POST.getlist("members")

        if not name:
            messages.error(request, "Team name is required")
            return redirect('core:teams')

        try:
            with transaction.atomic():

                team.name = name

                # ✅ Assign Lead
                lead = None
                if lead_id:
                    lead = User.objects.filter(id=lead_id).first()
                    team.lead = lead

                # ✅ Assign Members (exclude lead)
                members = User.objects.filter(id__in=member_ids)

                if lead:
                    members = members.exclude(id=lead.id)

                team.members.set(members)
                team.save()

                messages.success(request, "Team updated successfully")

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return redirect('core:teams')

@login_required
def delete_team(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    project = team.project

    if request.user != project.project_lead:
        messages.error(request, "Not allowed")
        return redirect('core:teams')

    if request.method == "POST":
        team_name = team.name

        try:
            team.delete()
            messages.success(request, f"Team '{team_name}' deleted")
        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return redirect('core:teams')

def get_status_counts(tasks):
    return {
        'todo': tasks.filter(status='todo').count(),
        'in_progress': tasks.filter(status='in_progress').count(),
        'in_review': tasks.filter(status='in_review').count(),
        'done': tasks.filter(status='done').count(),
    }

@login_required(login_url='core:login')
def project_board(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    user = request.user

    if not _project_accessible_by(user, project):
        return redirect('core:projects')

    role = user.profile.role
    
    # Role-Based Task Filtering
    if role == 'admin' or project.project_lead == user:
        # Admin & Project Lead → all tasks
        tasks = Task.objects.filter(project=project)
    
    elif role == 'delivery_team':
        # Delivery Team sees tasks of their team members (developers, testers, users)
        tasks = Task.objects.filter(
            project=project,
            assigned_to__profile__role__in=['developer', 'tester', 'ui_ux_designer', 'user']
        )
          
    else:  # regular team member
        tasks = Task.objects.filter(
            project=project,
            assigned_to=user
        )

    tasks = tasks.select_related('project', 'assigned_to').prefetch_related('labels').distinct()
    status_counts = get_status_counts(tasks)
    total_tasks = sum(status_counts.values())
    progress_percent = 0
    if total_tasks > 0:
        completed = status_counts.get('done', 0) + status_counts.get('in_review', 0)
        progress_percent = int((completed / total_tasks) * 100)

    board_tasks = list(tasks)
    for task in board_tasks:
        task.allowed_next_statuses = _task_allowed_next_statuses(user, task)
        task.allowed_next_status_labels = [_status_label(status) for status in task.allowed_next_statuses]

    context = {
        'project': project,
        'tasks': board_tasks,
        'status_choices': Task.STATUS_CHOICES,
        'status_counts': status_counts,
        'progress_percent': progress_percent,
        'can_manage_members': _can_manage_project_members(request.user, project),
        'invite_role_choices': Profile.ROLE_CHOICES,
        'workflow_rows': _workflow_transition_rows(),
        'role_label': _role_label(role),
    }
    return render(request, 'core/project_board.html', context)


@login_required(login_url='core:login')
def project_backlog(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not _project_accessible_by(request.user, project):
        return redirect('core:projects')

    qs = Task.objects.select_related('assigned_to').filter(project=project).order_by('status', '-id')
    if request.user.profile.role == 'delivery_team':
        qs = qs.filter(assigned_to__profile__role__in=['developer', 'tester', 'ui_ux_designer', 'user'])
    elif request.user.profile.role not in ['admin', 'project_lead']:
        qs = qs.filter(assigned_to=request.user)

    return render(request, 'core/project_backlog.html', {
        'project': project,
        'tasks': qs,
    })

@login_required
def project_team(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if not _project_accessible_by(request.user, project):
        return redirect('core:projects')

    total_tasks = Task.objects.filter(project=project).count()
    done_tasks = Task.objects.filter(project=project, status='done').count()

    progress_percent = 0
    if total_tasks > 0:
        progress_percent = int((done_tasks / total_tasks) * 100)

    # Get available users for adding to project
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.role == 'admin':
        # Admins can add any user
        available_users = User.objects.exclude(id__in=project.members.all()).exclude(id=project.created_by.id)
    else:
        # Project leads can only add users they're managing or similar role users
        available_users = User.objects.filter(
            Q(profile__role__in=['developer', 'tester']) |
            Q(profile__role='project_lead')
        ).exclude(id__in=project.members.all()).exclude(id=project.created_by.id)

    context = {
        'project': project,
        'done_tasks': done_tasks,
        'total_tasks': total_tasks,
        'progress_percent': progress_percent,
        'can_manage_members': _can_manage_project_members(request.user, project),
        'users': available_users
    }

    return render(request, 'core/project_team.html', context)

@login_required(login_url='core:login')
def user_dashboard(request):
    return redirect('core:dashboard')

from django.contrib.auth.models import User

@login_required(login_url='core:login')
def tasks(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    role = profile.role

    status_choices = [
        ('todo', 'To Do'),
        ('in_progress', 'In Progress'),
        ('in_review', 'In Review'),
        ('done', 'Done'),
    ]

    # ✅ GET filters
    project_id = request.GET.get('project')
    status = request.GET.get('status')
    user_id = request.GET.get('user')

    # 🔥 Base Query
    if role == 'admin':
        tasks = Task.objects.select_related('project', 'assigned_to').all()
        projects = Project.objects.all()
        users = User.objects.all()

    elif role in ['project_lead', 'delivery_team']:
        # ✅ Leads see all project tasks
        tasks = Task.objects.select_related('project', 'assigned_to').filter(
                  Q(project__members=request.user) |
                  Q(project__project_lead=request.user) 
        ).distinct()

        projects = Project.objects.filter(
            Q(members=request.user) |
            Q(project_lead=request.user)
        ).distinct()
        
        users = None

    else:
        tasks = Task.objects.select_related('project', 'assigned_to').filter(
            Q(project__members=request.user) | Q(assigned_to=request.user)
        ).distinct()

        projects = Project.objects.filter(members=request.user)
        users = None

    # ✅ Apply Filters (OUTSIDE role block → applies to all)
    if project_id:
        tasks = tasks.filter(project_id=project_id)

    if status:
        tasks = tasks.filter(status=status)

    if user_id and role == 'admin':
        tasks = tasks.filter(assigned_to_id=user_id)

    # ✅ Status count AFTER filtering
    status_counts = Counter(task.status for task in tasks)

    return render(request, 'core/tasks.html', {
        'tasks': tasks,
        'projects': projects,
        'selected_project': project_id,
        'selected_status': status,
        'selected_user': user_id,
        'users': users,
        'status_choices': status_choices,
        'status_counts': status_counts,
    })
    
from django.views.decorators.csrf import csrf_exempt



@login_required
@role_required(['developer'])
def start_task(request, task_id):
    # Backward-compatible endpoint (use unified transition)
    task = get_object_or_404(Task, id=task_id)
    if not _task_can_transition(request.user, task, 'in_progress'):
        return redirect('core:dashboard')
    old_status = task.status
    task.status = 'in_progress'
    task.save()
    _log_task_activity(task, request.user, "Status changed", _status_label(old_status), _status_label('in_progress'))
    return redirect('core:dashboard')


@login_required
@role_required(['developer'])
def submit_task(request, task_id):
    # Backward-compatible endpoint (use unified transition)
    task = get_object_or_404(Task, id=task_id)
    if not _task_can_transition(request.user, task, 'in_review'):
        return redirect('core:dashboard')
    old = task.status
    task.status = 'in_review'
    task.save()
    _log_task_activity(task, request.user, "Status changed", _status_label(old), _status_label('in_review'))
    return redirect('core:dashboard')

@login_required
@role_required(['tester'])
def approve_task(request, task_id):
    # Backward-compatible endpoint (use unified transition)
    task = get_object_or_404(Task, id=task_id)
    if not _task_can_transition(request.user, task, 'done'):
        return redirect('core:dashboard')
    old = task.status
    task.status = 'done'
    task.save()
    _log_task_activity(task, request.user, "Status changed", _status_label(old), _status_label('done'))
    return redirect('core:dashboard')

@login_required
def create_task(request):
    assignee_id = request.GET.get('assignee') or request.GET.get('assigned_to')
    selected_project = request.GET.get('project')

    if request.user.profile.role == 'admin':
        projects = Project.objects.all()
    else:
        projects = Project.objects.filter(
            Q(members=request.user) | Q(project_lead=request.user)
        ).distinct()
    users = User.objects.none()
    selected_assignee = None

    if assignee_id:
        try:
            selected_assignee = User.objects.get(id=assignee_id)
            projects = projects.filter(
                Q(members=selected_assignee) | Q(project_lead=selected_assignee)
            ).distinct()
        except User.DoesNotExist:
            selected_assignee = None

    # ✅ preload users if project selected (page load case)
    if selected_project:
        project = get_object_or_404(Project, id=selected_project)
        if not _project_accessible_by(request.user, project):
            return redirect('core:projects')
        users = project.members.all()
        if project.project_lead and project.project_lead not in users:
            users = users | User.objects.filter(id=project.project_lead.id)
        if selected_assignee and selected_assignee in users:
            pass
        elif selected_assignee and selected_assignee not in users:
            selected_assignee = None

    labels_qs = Label.objects.all().order_by("name")

    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        assigned_to_id = request.POST.get('assigned_to')
        project_id = request.POST.get('project')
        priority = request.POST.get('priority')
        due_date_raw = (request.POST.get('due_date') or '').strip()
        start_date_raw = (request.POST.get('start_date') or '').strip()
        delivery_date_raw = (request.POST.get('delivery_date') or '').strip()
        label_ids = request.POST.getlist('labels')

        # ❌ basic validation
        if not (title and assigned_to_id and project_id):
            messages.error(request, "All fields required")
            return redirect('core:create_task')

        project = get_object_or_404(Project, id=project_id)
        if request.user.profile.role != 'admin' and not _project_accessible_by(request.user, project):
            return redirect('core:projects')
        assigned_user = get_object_or_404(User, id=assigned_to_id)

        # 🔐 CORE SECURITY (JIRA RULE)
        if assigned_user not in project.members.all() and assigned_user != project.project_lead:
            messages.error(request, "User not part of this project")
            return redirect(f'/tasks/create/?project={project_id}')

        # Allocate project-scoped issue number (Jira-style key).
        issue_number = None
        if project:
            issue_number = project.next_issue_number
            project.next_issue_number = issue_number + 1
            project.save(update_fields=["next_issue_number"])

        due_dt = None
        if due_date_raw:
            try:
                y, m, d = [int(x) for x in due_date_raw.split("-")]
                due_dt = date(y, m, d)
            except Exception:
                messages.error(request, "Invalid due date")
                return redirect('core:create_task')

        start_dt = None
        if start_date_raw:
            try:
                y, m, d = [int(x) for x in start_date_raw.split("-")]
                start_dt = date(y, m, d)
            except Exception:
                pass  # Ignore invalid start date

        delivery_dt = None
        if delivery_date_raw:
            try:
                y, m, d = [int(x) for x in delivery_date_raw.split("-")]
                delivery_dt = date(y, m, d)
            except Exception:
                pass  # Ignore invalid delivery date

        task = Task.objects.create(
            title=title,
            description=description,
            assigned_to=assigned_user,
            created_by=request.user,
            project=project,
            priority=priority,
            issue_number=issue_number,
            start_date=start_dt,
            due_date=due_dt,
            delivery_date=delivery_dt,
        )

        # Labels (optional)
        cleaned_ids = [int(x) for x in label_ids if str(x).isdigit()]
        if cleaned_ids:
            task.labels.set(Label.objects.filter(id__in=cleaned_ids))

        messages.success(request, "Task created successfully")
        return redirect('core:tasks')

    return render(request, 'core/create_task.html', {
        'projects': projects,
        'users': users,
        'selected_project': selected_project,
        'selected_assignee': selected_assignee.id if selected_assignee else '',
        'labels': labels_qs,
    })
@login_required
def update_task_status(request, task_id, new_status):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)

    # 🔐 Permission checks
    if not _task_can_view(request.user, task):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized'}, status=403)
     
    # ✅ Soft rule (NOT strict workflow)
    if new_status == 'done' and request.user.profile.role != 'tester':
       return JsonResponse({
        'status': 'error',
        'message': 'Only testers can mark task as Done'
    }, status=403)

    # ⛔ No change
    if task.status == new_status:
        project_counts, progress_percent = _project_progress_snapshot(task.project)
        return JsonResponse({
            'status': 'no_change',
            'notification_count': _get_notification_count(request.user),
            'status_counts': project_counts,
            'progress_percent': progress_percent,
            'allowed_next_statuses': _task_allowed_next_statuses(request.user, task),
        })

    try:
        with transaction.atomic():
            old_status = task.status

            # 🔥 AUTO-ASSIGN TESTER WHEN MOVED TO IN_REVIEW
            assigned_old = task.assigned_to

            if new_status == 'in_review':
                testers = User.objects.filter(
                    profile__role='tester',
                    team_members__project=task.project
                ).distinct()

                if testers.exists():
                    # 👉 simple: assign first tester
                    task.assigned_to = testers.first()
                else:
                    # fallback
                    task.assigned_to = task.project.project_lead

            # 🔥 RESTRICT DONE → ONLY TESTER
            if new_status == 'done' and request.user.profile.role != 'tester':
                return JsonResponse({
                    'status': 'error',
                    'message': 'Only tester can mark task as Done'
                }, status=403)

            # ✅ Update status
            task.status = new_status
            task.save(update_fields=['status', 'assigned_to'])

            # 📝 Activity: status change
            _log_task_activity(
                task=task,
                user=request.user,
                action="Status changed",
                old_value=_status_label(old_status),
                new_value=_status_label(new_status),
            )

            # 📝 Activity: assignment change (only if changed)
            if assigned_old != task.assigned_to:
                _log_task_activity(
                    task=task,
                    user=request.user,
                    action="Assigned changed",
                    old_value=getattr(assigned_old, 'username', 'Unassigned'),
                    new_value=getattr(task.assigned_to, 'username', 'Unassigned'),
                )

        notification_count = _get_notification_count(request.user)
        project_counts, progress_percent = _project_progress_snapshot(task.project)

        return JsonResponse({
            'status': 'success',
            'old': old_status,
            'new': new_status,
            'assigned_to': task.assigned_to.username if task.assigned_to else None,
            'notification_count': notification_count,
            'status_counts': project_counts,
            'progress_percent': progress_percent,
            'allowed_next_statuses': _task_allowed_next_statuses(request.user, task),
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)

@login_required
def task_detail(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    if not _task_can_view(request.user, task):
        return redirect('core:tasks')

    role = request.user.profile.role
    next_statuses = _task_allowed_next_statuses(request.user, task)
    workflow_rows = [
        row for row in _workflow_transition_rows()
        if row["from_status"] == task.status
    ]

    return render(request, 'core/task_detail.html', {
        'task': task,
        'next_statuses': next_statuses,
        'role_label': _role_label(role),
        'workflow_rows': workflow_rows,
        'today': timezone.now().date(),
    })



@login_required
@role_required(['tester'])
def reject_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    # Backward-compatible endpoint (use unified transition)
    if not _task_can_transition(request.user, task, 'in_progress'):
        return redirect('core:dashboard')
    old = task.status
    task.status = 'in_progress'
    task.save()
    _log_task_activity(task, request.user, "Status changed", _status_label(old), _status_label('in_progress'))
    return redirect('core:dashboard')


@login_required
def get_comments(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    # ✅ Security (fix warning + protect data)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    comments = Comment.objects.filter(task=task).order_by('-created')

    data = [
        {
            "user": c.user.username,
            "text": c.text,
            "time": c.created.strftime("%d %b %H:%M")
        }
        for c in comments
    ]

    return JsonResponse(data, safe=False)


@login_required
def add_comment(request, task_id):
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid payload'}, status=400)

    text = body.get('text', '').strip()

    if not text:
        return JsonResponse({'error': 'Empty comment'}, status=400)

    comment = Comment.objects.create(
        task=task,
        user=request.user,
        text=text
    )

    _log_task_activity(
        task=task,
        user=request.user,
        action="Comment added",
        new_value=text[:100],
    )

    return JsonResponse({
        "status": "ok",
        "comment": {
            "user": comment.user.username,
            "text": comment.text,
            "time": comment.created.strftime("%d %b %H:%M"),
        }
    })


@login_required
def upload_attachment(request, task_id):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)
    file = request.FILES.get("file")

    if not file:
        return JsonResponse({'error': 'No file provided'}, status=400)

    attachment = TaskAttachment.objects.create(
        task=task,
        file=file,
        uploaded_by=request.user
    )

    _log_task_activity(task, request.user, "File uploaded", new_value=attachment.file.name.split('/')[-1])

    return JsonResponse({
        "status": "ok",
        "file": {
            "name": attachment.file.name.split('/')[-1],
            "url": attachment.file.url
        }
    })
    

@login_required
def task_activity(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    # ✅ use request (fix warning + add security)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    activities = TaskActivity.objects.filter(task=task).order_by('-created_at')

    return JsonResponse([
        {
            "user": a.user.username,
            "action": a.action,
            "old": a.old_value,
            "new": a.new_value,
            "time": a.created_at.strftime("%d %b %H:%M")
        }
        for a in activities
    ], safe=False)

@login_required
def get_files(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    # ✅ use request (important security)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    files = TaskAttachment.objects.filter(task=task)

    return JsonResponse([
        {
            "id": f.id,
            "name": f.file.name.split('/')[-1],
            "url": f.file.url,
            "user": f.uploaded_by.username
        }
        for f in files
    ], safe=False)


@login_required
def delete_attachment(request, task_id, attachment_id):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    attachment = get_object_or_404(TaskAttachment, id=attachment_id, task=task)
    name = attachment.file.name.split('/')[-1] if attachment.file else "file"
    attachment.delete()

    _log_task_activity(task, request.user, "File removed", old_value=name)

    return JsonResponse({"status": "deleted"})


@login_required
def update_task_due_date(request, task_id):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    due_raw = (request.POST.get("due_date") or "").strip()
    if not due_raw:
        old_due = task.due_date.isoformat() if task.due_date else ""
        task.due_date = None
        task.save(update_fields=["due_date"])
        _log_task_activity(task, request.user, "Due date cleared", old_value=old_due)
        return JsonResponse({"status": "ok", "due_date": ""})

    try:
        # HTML date input format: YYYY-MM-DD
        y, m, d = [int(x) for x in due_raw.split("-")]
        new_due = date(y, m, d)
    except Exception:
        return JsonResponse({"error": "Invalid date"}, status=400)

    old = task.due_date.isoformat() if task.due_date else ""
    task.due_date = new_due
    task.save(update_fields=["due_date"])
    _log_task_activity(task, request.user, "Due date changed", old, new_due.isoformat())
    return JsonResponse({"status": "ok", "due_date": new_due.isoformat()})


@login_required
def update_task_labels(request, task_id):
    if request.method != "POST":
        return JsonResponse({'error': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)
    if not _task_can_view(request.user, task):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    raw = (request.POST.get("labels") or "").strip()
    # Accept comma-separated label IDs.
    ids = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))

    old_names = ", ".join(task.labels.values_list("name", flat=True))
    task.labels.set(ids)
    new_names = ", ".join(task.labels.values_list("name", flat=True))

    _log_task_activity(task, request.user, "Labels updated", old_names, new_names)
    return JsonResponse({
        "status": "ok",
        "labels": [{"id": l.id, "name": l.name, "color": l.color} for l in task.labels.all()],
    })
    
    
import time
from datetime import timedelta

def _check_login_attempts(request):
    """Rate limiting: max 5 attempts per 15 minutes"""
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
    key = f'login_attempts_{ip}'
    
    attempts = request.session.get(key, 0)
    last_attempt = request.session.get(f'{key}_time')

    if last_attempt:
        elapsed = time.time() - last_attempt   # ✅ FIX
        if elapsed > 900:  # 15 minutes = 900 seconds
            request.session[key] = 0
            request.session.pop(f'{key}_time', None)
            return False

    return attempts >= 5


def _increment_login_attempts(request):
    """Increment login attempt counter"""
    ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', ''))
    key = f'login_attempts_{ip}'
    
    request.session[key] = request.session.get(key, 0) + 1
    request.session[f'{key}_time'] = time.time()   # ✅ FIX

def login_view(request):
    if request.method == "POST":
        # Rate limiting check
        if _check_login_attempts(request):
            messages.error(request, "Too many login attempts. Please try again in 15 minutes.")
            return render(request, "core/auth.html", {"initialTab": "login"})
        
        raw_login = (request.POST.get("username") or "").strip()
        password = (request.POST.get("password") or "").strip()

        if not raw_login or not password:
            messages.error(request, "Username/email and password are required.")
            _increment_login_attempts(request)
            return render(request, "core/auth.html", {"initialTab": "login"})

        username = raw_login
        if "@" in raw_login:
            try:
                validate_email(raw_login)
                matched_user = User.objects.filter(email__iexact=raw_login).first()
                if matched_user:
                    username = matched_user.username
            except ValidationError:
                messages.error(request, "Invalid email format.")
                _increment_login_attempts(request)
                return render(request, "core/auth.html", {"initialTab": "login"})

        user = authenticate(request, username=username, password=password)

        if user is not None:
            # ✅ FIX: Ensure profile exists
            profile, created = Profile.objects.get_or_create(user=user)
            # Clear attempt counter on success
            ip = request.META.get('REMOTE_ADDR', '')
            request.session.pop(f'login_attempts_{ip}', None)

            login(request, user)
            messages.success(request, f"Welcome back, {user.username}!")

            # Role-based redirect (Jira-style)
            if profile.role == 'admin':
                return redirect('core:dashboard')
            elif profile.role in ['project_lead', 'team_lead']:
                return redirect('core:projects')
            else:
                return redirect('core:home')

        else:
            _increment_login_attempts(request)
            messages.error(request, "Invalid username/email or password. Please try again.")

    return render(request, "core/auth.html", {"initialTab": "login"})


def is_project_lead(user, project):
    return (
        user.profile.role == 'project_lead' or
        project.project_lead_id == user.id
    )


# ✅ REGISTER with validation
# def register_view(request):
#     if request.method == "POST":
#         username = (request.POST.get("username") or "").strip()
#         email = (request.POST.get("email") or "").strip().lower()
#         password = (request.POST.get("password") or "").strip()
#         confirm_password = (request.POST.get("confirm_password") or "").strip()

#         # Validation
#         if not all([username, email, password]):
#             messages.error(request, "All fields are required.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         if len(username) < 3:
#             messages.error(request, "Username must be at least 3 characters.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         if len(password) < 8:
#             messages.error(request, "Password must be at least 8 characters.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         if password != confirm_password:
#             messages.error(request, "Passwords do not match.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         try:
#             validate_email(email)
#         except ValidationError:
#             messages.error(request, "Please enter a valid email address.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         if User.objects.filter(username__iexact=username).exists():
#             messages.error(request, "Username already exists.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         if User.objects.filter(email__iexact=email).exists():
#             messages.error(request, "Email already registered.")
#             return render(request, "core/auth.html", {"initialTab": "register"})

#         try:
#             user = User.objects.create_user(username=username, email=email, password=password)
#             user.save()

#             # default role
#             profile, _ = Profile.objects.get_or_create(user=user)
#             profile.role = 'user'
#             profile.save()

#             login(request, user)
#             messages.success(request, f"Welcome to TaskForge, {username}! Your account has been created.")
#             return redirect('core:home')
#         except Exception as e:
#             messages.error(request, f"An error occurred during registration. Please try again.")
            
#     return render(request, "core/auth.html", {"initialTab": "register"})

def logout_view(request):
    logout(request)
    messages.success(request, "Logged out successfully")
    return redirect('core:home')


@login_required(login_url='core:login')
def role_redirect(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if profile.role == 'admin':
        return redirect('core:dashboard')

    elif profile.role in ['project_lead', 'team_lead']:
        return redirect('core:projects')

    elif profile.role == 'guest':
        return redirect('core:guest_dashboard')  # 👈 NEW

    else:
        return redirect('core:home')


# ✅ PASSWORD RESET REQUEST (Jira-style)
def password_reset_request(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        try:
            validate_email(email)
            user = User.objects.filter(email__iexact=email).first()
            if user:
                # Generate reset token (in production, send via email)
                reset_token = secrets.token_urlsafe(32)
                request.session[f'reset_token_{user.id}'] = reset_token
                request.session.set_expiry(3600)  # 1 hour expiry
                messages.success(request, f"Password reset instructions have been sent to your email.")
            else:
                # Don't reveal if email exists (security best practice)
                messages.info(request, f"If an account exists with that email, you will receive reset instructions.")
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
    return render(request, "core/auth.html", {"initialTab": "login"})


from django.db.models import Q

@login_required(login_url='core:login')
def search_view(request):
    query = request.GET.get('q', '').strip()
    profile, _ = Profile.objects.get_or_create(user=request.user)
    role = profile.role

    results = []

    if query:
        # Build base queryset based on role
        if role == 'admin':
            base_tasks = Task.objects.all()
            base_projects = Project.objects.all()
        elif role == 'project_lead':
            base_tasks = Task.objects.filter(
                Q(project__project_lead=request.user) | Q(assigned_to=request.user)
            )
            base_projects = Project.objects.filter(project_lead=request.user)
        else:
            base_tasks = Task.objects.filter(assigned_to=request.user)
            base_projects = Project.objects.filter(members=request.user)

        combined_ids = set()
        combined_results = []

        # Search by exact ID if query is numeric
        if query.isdigit():
            tasks_by_id = base_tasks.filter(id=int(query))
            for task in tasks_by_id:
                if task.id not in combined_ids:
                    combined_ids.add(task.id)
                    combined_results.append({
                        'id': task.id,
                        'title': task.title,
                        'issue_key': task.issue_key,
                        'project_name': task.project.name,
                        'status': task.status,
                        'type': 'task'
                    })

        # Search by issue_key prefix (e.g., "TF-1" or "TF")
        tasks_by_key = base_tasks.filter(issue_key__icontains=query)
        for task in tasks_by_key:
            if task.id not in combined_ids:
                combined_ids.add(task.id)
                combined_results.append({
                    'id': task.id,
                    'title': task.title,
                    'issue_key': task.issue_key,
                    'project_name': task.project.name,
                    'status': task.status,
                    'type': 'task'
                })

        # Search by task title
        tasks_by_title = base_tasks.filter(title__icontains=query)
        for task in tasks_by_title:
            if task.id not in combined_ids:
                combined_ids.add(task.id)
                combined_results.append({
                    'id': task.id,
                    'title': task.title,
                    'issue_key': task.issue_key,
                    'project_name': task.project.name,
                    'status': task.status,
                    'type': 'task'
                })

        # Search by project name
        tasks_by_project = base_tasks.filter(project__name__icontains=query)
        for task in tasks_by_project:
            if task.id not in combined_ids:
                combined_ids.add(task.id)
                combined_results.append({
                    'id': task.id,
                    'title': task.title,
                    'issue_key': task.issue_key,
                    'project_name': task.project.name,
                    'status': task.status,
                    'type': 'task'
                })

        # Also search projects by name
        projects = base_projects.filter(name__icontains=query)
        for project in projects[:5]:
            combined_results.append({
                'id': project.id,
                'title': project.name,
                'issue_key': None,
                'project_name': project.name,
                'status': None,
                'type': 'project'
            })

        results = combined_results[:10]

    return JsonResponse({'results': results})



@login_required
@role_required(['admin'])
def create_project(request):
    users = User.objects.all()
    
    if request.method == 'POST':
        name = request.POST.get('name')
        description = request.POST.get('description')
        project_type = request.POST.get('project_type') or 'kanban'
        key_prefix = (request.POST.get('key_prefix') or 'TF').strip().upper()[:10] or 'TF'
        project_lead_id = request.POST.get('project_lead')
        project_lead = User.objects.filter(id=project_lead_id).first()
        
        
        project = Project.objects.create(
            name=name,
            description=description,
            created_by=request.user,
            project_type=project_type,
            key_prefix=key_prefix,
            project_lead=project_lead  
        )
    
        project.members.add(request.user)
        if project_lead:
            project.members.add(project_lead)

        return redirect('core:projects')

    return render(request, 'core/create_project.html', {
        'users': users  
    })


@login_required
def projects(request):
    profile = request.user.profile
    role = profile.role

    # =========================
    # ✅ CREATE PROJECT (POST)
    # =========================
    if request.method == "POST":

        name = request.POST.get('name')
        description = request.POST.get('description')
        project_type = request.POST.get('project_type')
        key_prefix = request.POST.get('key_prefix')
        lead_id = request.POST.get('project_lead')

        if not name:
            return JsonResponse({'error': 'Project name required'}, status=400)

        # ✅ Validate lead
        lead = None
        if lead_id:
            try:
                lead = User.objects.get(id=lead_id)
            except User.DoesNotExist:
                return JsonResponse({'error': 'Invalid lead'}, status=400)

        # ✅ Create project
        project = Project.objects.create(
            name=name,
            description=description,
            created_by=request.user,
            project_type=project_type,
            key_prefix=key_prefix,
            project_lead=lead
        )

        # ✅ Members
        project.members.add(request.user)
        if lead:
            project.members.add(lead)

        # ✅ RETURN JSON (NO redirect here)
        return JsonResponse({
            'status': 'ok',
            'project': {
                'id': project.id,
                'name': project.name,
                'description': project.description or "No description",
                'lead': lead.username if lead else "",
            }
        })

    # =========================
    # ✅ VIEW PROJECTS (GET)
    # =========================

    if role == 'admin':
        projects = Project.objects.prefetch_related('members')
        users = User.objects.all()

    elif role in ['project_lead', 'team_lead']:
        projects = Project.objects.filter(
            Q(members=request.user) |
            Q(project_lead=request.user)
        ).distinct().prefetch_related('members')

        users = User.objects.filter(is_active=True)

    else:
        projects = Project.objects.filter(
            members=request.user
        ).prefetch_related('members')

        users = User.objects.filter(is_active=True)

    return render(request, 'core/projects.html', {
        'projects': projects,
        'users': users
    })

@login_required
@role_required(['admin'])
def edit_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if request.method == 'POST':
        project.name = request.POST.get('name')
        project.description = request.POST.get('description')
        project.save()

        return redirect('core:projects')

    return render(request, 'core/edit_project.html', {'project': project})

@login_required
@role_required(['admin'])
def delete_project(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if request.method == 'POST':
        project.delete()
        return redirect('core:projects')

    return render(request, 'core/delete_project.html', {'project': project})



# ADD THESE IMPORTS
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.urls import reverse
from django.conf import settings

@login_required
def invite_project_member(request, project_id):

    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    project = get_object_or_404(Project, id=project_id)

    # ✅ Permission check
    if not _can_manage_project_members(request.user, project):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    email = (request.POST.get('email') or '').strip().lower()
    role = (request.POST.get('role') or 'user').strip()
    team_id = request.POST.get('team_id')

    # =============================
    # ✅ VALIDATION
    # =============================
    if not email:
        return JsonResponse({'error': 'Email is required'}, status=400)

    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({'error': 'Invalid email address'}, status=400)

    valid_roles = {c[0] for c in Profile.ROLE_CHOICES}

    # ❌ Block admin assignment
    if role == 'admin':
        return JsonResponse({'error': 'Cannot assign admin role'}, status=400)

    if role not in valid_roles:
        role = 'user'

    # =============================
    # ✅ TEAM VALIDATION
    # =============================
    team = None
    if team_id:
        team = Team.objects.filter(id=team_id, project=project).first()
        if not team:
            return JsonResponse({'error': 'Invalid team selected'}, status=400)

    try:
        with transaction.atomic():

            existing_user = User.objects.filter(email__iexact=email).first()

            # =============================
            # ✅ EXISTING USER FLOW
            # =============================
            if existing_user:
                user = existing_user

                # 🔹 Case 1: Already in project
                if project.members.filter(id=user.id).exists():

                    # 👉 Add to team if not already
                    if team and not team.members.filter(id=user.id).exists():
                        team.members.add(user)

                        return JsonResponse({
                            'status': 'added_to_team',
                            'message': 'User added to team',
                            'user': {
                                'id': user.id,
                                'username': user.username,
                                'email': user.email,
                                'role': getattr(user.profile, 'role', 'user')
                            },
                            'team': team.name if team else None
                        })

                    return JsonResponse({
                        'error': 'User already in project'
                    }, status=400)

                # 🔹 Case 2: Not in project → add
                project.members.add(user)

                if team:
                    team.members.add(user)

                return JsonResponse({
                    'status': 'added',
                    'message': 'User added to project',
                    'user': {
                        'id': user.id,
                        'username': user.username,
                        'email': user.email,
                        'role': getattr(user.profile, 'role', 'user')
                    },
                    'team': team.name if team else None
                })

            # =============================
            # ✅ NEW USER INVITE FLOW
            # =============================
            existing_invite = ProjectInvite.objects.filter(
                email=email,
                project=project,
                used=False
            ).first()

            if existing_invite:
                return JsonResponse({'error': 'Invite already sent'}, status=400)

            token = secrets.token_urlsafe(32)

            invite = ProjectInvite.objects.create(
                email=email,
                role=role,
                project=project,
                token=token,
                team=team
            )

            # ✅ BUILD ACCEPT LINK
            accept_url = request.build_absolute_uri(
                  reverse('core:accept_project_invite',args=[invite.token])
            )
            
            # Convert role value → label
            role_display = dict(Profile.ROLE_CHOICES).get(role, 'User')
             # ✅ EMAIL CONTEXT
            context = {
                "project": project,
                 "inviter": request.user,
                 "accept_url": accept_url,
                  "role": role_display,
            }

# ✅ RENDER EMAIL
            html_content = render_to_string("emails/invite_email.html", 
                                            context)
            text_content = strip_tags(html_content)

# ✅ SEND EMAIL
            email_msg = EmailMultiAlternatives(
                subject=f"You're invited to join {project.name}",
                body=text_content,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[email],
            )

            email_msg.attach_alternative(html_content, "text/html")   
            email_msg.send()

            return JsonResponse({
                 'status': 'invited',
                 'message': 'Invite sent successfully',
                 'user': {
                 'email': email,
                'role': role
            },
                'team': team.name if team else None
            })
    except Exception as e:
        return JsonResponse({
            'error': str(e)  # use generic msg in production
        }, status=500)


@login_required
def accept_project_invite(request, token):
    invite = get_object_or_404(ProjectInvite, token=token, used=False)

    # ✅ Email match check
    if request.user.email.lower() != invite.email.lower():
        messages.error(request, "This invite is not for your email.")
        return redirect('core:login')

    # ✅ Add to project
    invite.project.members.add(request.user)

    # ✅ Add to team if exists
    if invite.team:
        invite.team.members.add(request.user)

    # ✅ Role assignment (safe)
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not profile.role or profile.role == 'user':
        profile.role = invite.role
        profile.save()

    # ✅ Mark used
    invite.used = True
    invite.save()

    messages.success(
        request,
        f"You joined '{invite.project.name}' as {invite.role}"
    )

    return redirect('core:project_board', project_id=invite.project.id)


@login_required
def add_project_member(request, project_id):
    if request.method == "POST":
        project = get_object_or_404(Project, id=project_id)
        if not _can_manage_project_members(request.user, project):
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        user_id = request.POST.get('user_id')
        if not user_id:
            return JsonResponse({'error': 'User ID required'}, status=400)
            
        user = get_object_or_404(User, id=user_id)
        
        # Check if user is already a member
        if project.members.filter(id=user.id).exists():
            return JsonResponse({'error': 'User is already a member'}, status=400)
        
        # Add the user to the project
        project.members.add(user)
        
        return JsonResponse({"status": "added"})

    return JsonResponse({'error': 'Invalid method'}, status=400)


@login_required
def remove_project_member(request, project_id, user_id):
    if request.method == "POST":
        project = get_object_or_404(Project, id=project_id)
        if not _can_manage_project_members(request.user, project):
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        user = get_object_or_404(User, id=user_id)

        # ❌ cannot remove creator
        if project.created_by == user:
            return JsonResponse({'error': 'Cannot remove owner'}, status=400)

        # ❌ cannot remove if tasks exist
        if Task.objects.filter(project=project, assigned_to=user).exists():
            return JsonResponse({'error': 'User has tasks'}, status=400)

        project.members.remove(user)

        return JsonResponse({"status": "removed"})

    return JsonResponse({'error': 'Invalid'}, status=400)


@login_required
def get_project_progress(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if not _project_accessible_by(request.user, project):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    tasks = Task.objects.filter(project=project)
    total_tasks = tasks.count()
    done_tasks = tasks.filter(status='done').count()
    in_review_tasks = tasks.filter(status='in_review').count()
    completed_count = done_tasks + in_review_tasks

    progress_percent = 0
    if total_tasks > 0:
        progress_percent = int((completed_count / total_tasks) * 100)

    return JsonResponse({
        'status': 'ok',
        'total_tasks': total_tasks,
        'done_tasks': completed_count,
        'progress_percent': progress_percent
    })


@login_required
def get_project_members(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if not _project_accessible_by(request.user, project):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    members = list(project.members.values('id', 'username'))
    return JsonResponse(members, safe=False)

@login_required
def get_project_members_api(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if not _project_accessible_by(request.user, project):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    members = project.members.values('id', 'username')

    return JsonResponse(list(members), safe=False)


from .models import Team  # ✅ add this

@login_required
def project_detail(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    # =========================
    # ✅ ACCESS CONTROL
    # =========================
    if not _project_accessible_by(request.user, project):
        return redirect('core:projects')

    # =========================
    # ✅ TEAM FETCH
    # =========================
    teams = Team.objects.filter(project=project).prefetch_related('members', 'lead')

    team_id = request.GET.get('team')
    selected_team = None

    if team_id:
        selected_team = teams.filter(id=team_id).first()

    # =========================
    # ✅ ROLE-BASED TASK FILTER
    # =========================
    user = request.user
    role = user.profile.role

    if project.project_lead == user or role == 'admin':
        # 🔹 Full access
        tasks = Task.objects.filter(project=project)

    elif role == 'delivery_team':
        # 🔹 Only teams led by this user
        my_teams = teams.filter(lead=user)

        # ❌ Prevent accessing other teams via URL
        if selected_team and selected_team not in my_teams:
            return JsonResponse({'error': 'Unauthorized team access'}, status=403)

        if selected_team:
            tasks = Task.objects.filter(
                project=project,
                assigned_to__in=selected_team.members.all()
            )

        else:
            tasks = Task.objects.filter(
                project=project,
                assigned_to__in=User.objects.filter(
                    teams__in=my_teams
                )
            ).distinct()

    else:
        # 🔹 Normal user → only own tasks
        tasks = Task.objects.filter(
            project=project,
            assigned_to=user
        )

    # =========================
    # ✅ OPTIMIZATION
    # =========================
    tasks = tasks.select_related('assigned_to')

    # =========================
    # ✅ STATUS COUNTS
    # =========================
    status_counts = Counter(task.status for task in tasks)
    task_count = tasks.count()

    progress_percent = 0
    if task_count > 0:
        completed_count = (
            status_counts.get('done', 0) +
            status_counts.get('in_review', 0)
        )
        progress_percent = int((completed_count / task_count) * 100)

    # =========================
    # ✅ PERMISSIONS
    # =========================
    can_manage = _can_manage_project_members(user, project)
    is_project_lead = project.project_lead == user or role == 'admin'

    # =========================
    # ✅ MEMBER FILTER (FIXED)
    # =========================
    if selected_team:
        valid_members = selected_team.members.exclude(
            profile__role__in=['project_lead', 'admin']
        )
    else:
        valid_members = project.members.exclude(
            profile__role__in=['project_lead', 'admin']
        ).distinct()

    # =========================
    # ✅ LEAD DROPDOWN FIX
    # =========================
    if selected_team:
        lead_candidates = selected_team.members.filter(
            profile__role__in=['team_lead', 'developer']
        )
    else:
        lead_candidates = project.members.filter(
            profile__role__in=['team_lead', 'developer']
        ).distinct()

    # =========================
    # ✅ FINAL RESPONSE
    # =========================
    return render(request, 'core/project_detail.html', {
        'project': project,
        'tasks': tasks,
        'teams': teams,
        'selected_team': selected_team,
        'status_counts': status_counts,
        'can_manage_members': can_manage,
        'is_project_lead': is_project_lead,
        'progress_percent': progress_percent,
        'valid_members': valid_members,
        'lead_candidates': lead_candidates,  # 🔥 important for UI
    })


@login_required
def toggle_user(request, id):
    user = get_object_or_404(User, id=id)
    user.is_active = not user.is_active
    user.save()
    return JsonResponse({"status": "ok"})


def user_stats(request):
    return JsonResponse({
        "total": User.objects.count(),
        "admins": User.objects.filter(is_superuser=True).count(),
        "active": User.objects.filter(is_active=True).count()
    })

#  USER LIST

@login_required
@role_required(['admin'])
def user_list(request):
    

    # ✅ Safe profile access
    profile = getattr(request.user, 'profile', None)

    if not profile or profile.role != 'admin':
        return redirect('core:home')

    query = request.GET.get('q', '')

    # ✅ Optimized query
    users = User.objects.select_related('profile').all().order_by('-id')

    if query:
        users = users.filter(username__icontains=query)

    paginator = Paginator(users, 5)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    total_users = users.count()
    total_admins = User.objects.filter(is_superuser=True).count()
    active_users = User.objects.filter(is_active=True).count()

    return render(request, 'core/user_list.html', {
        'page_obj': page_obj,
        'query': query,
        'total_admins': total_admins,
        'active_users': active_users,
        'total_users': total_users,
        # 'notification_count': _notification_count(request.user),
    })

#  CREATE USER

from django.contrib import messages
@login_required
@role_required(['admin'])
def user_create(request):

    profile = getattr(request.user, 'profile', None)
    if not profile or profile.role != 'admin':
        return redirect('core:home')

    if request.method == "POST":
        form = UserCreateForm(request.POST)

        if form.is_valid():
            try:
                with transaction.atomic():  # ✅ SAFE SAVE

                    username = form.cleaned_data['username']
                    email = form.cleaned_data['email']
                    password = form.cleaned_data['password']
                    role = form.cleaned_data['role']
                    is_active = form.cleaned_data.get('is_active', True)

                    # ✅ EXTRA VALIDATION
                    if User.objects.filter(username=username).exists():
                        form.add_error('username', 'Username already exists')
                        raise Exception("Validation failed")

                    if User.objects.filter(email=email).exists():
                        form.add_error('email', 'Email already exists')
                        raise Exception("Validation failed")

                    # ✅ CREATE USER
                    user = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password
                    )
                    user.is_active = is_active
                    user.save()

                    # ✅ PROFILE + ROLE
                    user_profile, _ = Profile.objects.get_or_create(user=user)
                    user_profile.role = role
                    user_profile.save()

                    messages.success(request, f"User '{username}' created successfully 🚀")
                    return redirect('core:user_list')

            except Exception:
                messages.error(request, "Please fix the errors below")

        else:
            messages.error(request, "Form validation failed")

    else:
        form = UserCreateForm()

    return render(request, 'core/user_create.html', {
        'form': form,
        # 'notification_count': _notification_count(request.user),
    })


@login_required
@role_required(['admin'])
def user_update(request, id):
    
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.role != 'admin':
        return redirect('core:home')

    user = get_object_or_404(User, id=id)

    if request.method == "POST":
        form = UserUpdateForm(request.POST, instance=user)
        if form.is_valid():
            user = form.save()

            # ✅ SAFE PROFILE UPDATE
            user_profile, _ = Profile.objects.get_or_create(user=user)
            role = form.cleaned_data["role"]
            user_profile.role = role
            user_profile.save()
            
            # ✅ PROJECT ASSIGNMENT
            assigned_projects = form.cleaned_data.get("assigned_projects", [])
            user.project_members.set(assigned_projects)
            
            messages.success(request, f"User {user.username} updated successfully with role '{role}' and {assigned_projects.count()} assigned projects.")
            return redirect("core:user_list")
    else:
        form = UserUpdateForm(instance=user)

    return render(request, "core/user_update.html", {
        "form": form,
        "user_obj": user,
        # 'notification_count': _notification_count(request.user),
    })


@login_required
@role_required(['admin'])
def user_delete(request, id):
    
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if profile.role != 'admin':
        return redirect('core:home')

    user = get_object_or_404(User, id=id)

    # Prevent deleting yourself
    if request.user == user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("core:user_list")

    if request.method == "POST":
        username = user.username
        user.delete()
        messages.success(request, f"User '{username}' deleted successfully.")
        return redirect("core:user_list")

    return render(request, "core/user_delete.html", {
        "user": user,
        # 'notification_count': _notification_count(request.user),
    })
    
    
from django.http import HttpResponse
import csv

@login_required
def reports_view(request):
    profile = request.user.profile
    role = profile.role

    # 🔍 Filters
    project_id = request.GET.get('project')
    status = request.GET.get('status')
    user_id = request.GET.get('user')

    # 🔥 Base Query (role-based)
    if role == 'admin':
        tasks = Task.objects.select_related('project', 'assigned_to').all()
        projects = Project.objects.all()
        users = User.objects.all()
    else:
        tasks = Task.objects.select_related('project', 'assigned_to').filter(
            Q(project__members=request.user) | Q(assigned_to=request.user)
        ).distinct()
        projects = Project.objects.filter(members=request.user)
        users = None

    # 🎯 Apply Filters
    if project_id:
        tasks = tasks.filter(project_id=project_id)

    if status:
        tasks = tasks.filter(status=status)

    if user_id and role == 'admin':
        tasks = tasks.filter(assigned_to_id=user_id)

    # 📊 Aggregations
    status_summary = {
        'todo': tasks.filter(status='todo').count(),
        'in_progress': tasks.filter(status='in_progress').count(),
        'in_review': tasks.filter(status='in_review').count(),
        'done': tasks.filter(status='done').count(),
    }

    project_summary = []
    for p in projects:
        p_tasks = tasks.filter(project=p)
        total = p_tasks.count()
        done = p_tasks.filter(status='done').count()

        project_summary.append({
            'project': p,
            'total': total,
            'completed': done,
            'progress': int((done / total) * 100) if total else 0
        })

    # 📤 EXPORT CSV
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="report.csv"'

        writer = csv.writer(response)
        writer.writerow(['Task', 'Project', 'Assigned To', 'Status', 'Priority'])

        for t in tasks:
            writer.writerow([
                t.title,
                t.project.name if t.project else '',
                t.assigned_to.username if t.assigned_to else '',
                t.status,
                t.priority
            ])

        return response

    return render(request, 'core/reports.html', {
        'tasks': tasks[:50],  # limit for UI
        'projects': projects,
        'users': users,
        'status_summary': status_summary,
        'project_summary': project_summary,
        'selected_project': project_id,
        'selected_status': status,
        'selected_user': user_id,
    })


def _delayed_tasks_count(user):
    from datetime import date

    if user.profile.role == 'admin':
        return Task.objects.filter(
            due_date__lt=date.today()
        ).exclude(status='done').count()

    return Task.objects.filter(
        due_date__lt=date.today(),
        assigned_to=user
    ).exclude(status='done').count()


# ================== NOTIFICATIONS ==================

@login_required
def get_notifications(request):
    """AJAX endpoint to get pending notifications for the current user"""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)
    
    # Get all unread notifications
    notifications = Notification.objects.filter(
        user=request.user,
        is_read=False
    ).order_by('-created_at')[:10]
    
    data = {
        'status': 'ok',
        'count': notifications.count(),
        'notifications': [
            {
                'id': n.id,
                'type': n.notification_type,
                'title': n.title,
                'message': n.message,
                'created_at': n.created_at.isoformat(),
                'task_id': n.task_id,
                'project_id': n.project_id,
            }
            for n in notifications
        ]
    }
    return JsonResponse(data)


@login_required
def mark_notification_read(request, notification_id):
    """AJAX endpoint to mark a single notification as read"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)
    
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save()
    
    # Count remaining unread notifications
    remaining = Notification.objects.filter(user=request.user, is_read=False).count()
    
    return JsonResponse({
        'status': 'ok',
        'remaining_count': remaining
    })


@login_required
def mark_all_notifications_read(request):
    """AJAX endpoint to mark all notifications as read"""
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)
    
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    
    return JsonResponse({
        'status': 'ok',
        'message': 'All notifications marked as read'
    })
    
