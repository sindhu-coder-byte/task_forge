from collections import Counter
from datetime import date, timedelta
import json
import secrets
import time
import csv
from django.shortcuts import get_object_or_404, render, redirect
from django.contrib.auth import login
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth import authenticate, logout
from django.http import HttpResponseForbidden, JsonResponse, HttpResponse
from django.core.paginator import Paginator
from django.core.signing import TimestampSigner, BadSignature, SignatureExpired, dumps as signing_dumps, loads as signing_loads
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.core.cache import cache
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.db.models import Q, F, Case, When, Value, IntegerField
from django.db.models import Count
from django.utils import timezone
from django.conf import settings

from .models import (
    Project, Task, Profile, Comment, TaskActivity, TaskAttachment,
    Label, ProjectInvite, Notification, Team, DepartmentPipelineSettings,
    ITDepartment, ITTeam, ProjectStageConfiguration,
)
from .forms import UserCreateForm, UserUpdateForm

from .models import ProjectMembership
from .notifications import NotificationService

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


def _is_project_lead(user) -> bool:
    profile = getattr(user, 'profile', None)
    if profile and (profile.role == 'project_lead' or profile.isProjectLead):
        return True
    return ProjectMembership.objects.filter(user=user, role='project_lead').exists()


def _is_project_lead_for_project(user, project: Project) -> bool:
    if user == project.project_lead:
        return True
    profile = getattr(user, 'profile', None)
    if profile and (profile.role == 'project_lead' or profile.isProjectLead):
        return True
    return ProjectMembership.objects.filter(
        user=user,
        project=project,
        role='project_lead'
    ).exists()


def _project_accessible_by(user, project: Project) -> bool:
    if not user.is_authenticated:
        return False

    profile = getattr(user, 'profile', None)
    if not profile:
        return False

    # Must stay in sync with the projects-list queryset (views.projects).
    return (
        profile.role == 'admin' or
        project.project_lead == user or
        project.created_by == user or
        project.members.filter(id=user.id).exists() or
        ProjectMembership.objects.filter(
            user=user, project=project, role='project_lead'
        ).exists()
    )
    
    
# ============================================================
# TEAM MEMBER FILTER HELPERS
# ============================================================

def _department_users_for_project(project):
    """
    Users belonging to same department as project.
    Falls back to all current project members when no department is set.
    """
    if project.department:
        return User.objects.filter(
            projectmembership__project__department=project.department,
            is_active=True
        ).select_related('profile').distinct()
    # No department: show all active project members so team creation still works
    return project.members.filter(is_active=True).select_related('profile')


def _project_team_candidates(project):

    users = _department_users_for_project(project)

    enriched = []

    for user in users:

        profile = getattr(user, 'profile', None)

        auth_type = (
            "Google"
            if profile and profile.oauth_provider == 'google'
            else "Manual"
        )

        membership = ProjectMembership.objects.filter(
            user=user,
            project=project
        ).first()

        role = membership.role if membership else "developer"

        enriched.append({
            'user': user,
            'role': role,
            'auth_type': auth_type,
            'department': project.department,
        })

    return enriched


def _can_manage_project_members(user, project: Project) -> bool:
    if not user.is_authenticated:
        return False

    profile = getattr(user, 'profile', None)
    if not profile:
        return False

    return (
        profile.role == 'admin' or
        project.project_lead_id == user.id or
        project.created_by_id == user.id or
        ProjectMembership.objects.filter(user=user, project=project, role='project_lead').exists()
    )


def _teams_led_by(user, project=None):
    qs = Team.objects.filter(lead=user)
    if project is not None:
        qs = qs.filter(project=project)
    return qs


def _team_scope_users(teams):
    return User.objects.filter(Q(teams__in=teams) | Q(leading_teams__in=teams)).distinct()


def _task_can_view(user, task: Task) -> bool:
    role = getattr(user.profile, "role", "user")
    project_role = None
    if task.project_id:
        project_role = ProjectMembership.objects.filter(
            user=user,
            project=task.project,
        ).values_list('role', flat=True).first()

    # Admin → everything
    if role == 'admin':
        return True

    # Project Lead → all project tasks
    profile = getattr(user, 'profile', None)
    if task.project and (
        task.project.project_lead == user or
        project_role == 'project_lead' or
        (profile and (profile.role == 'project_lead' or profile.isProjectLead))
    ):
        return True

    # Delivery Team → all project tasks
    if project_role == 'delivery_team' and task.project and task.project.members.filter(id=user.id).exists():
        return True

    # Team Member → ONLY assigned tasks
    return task.assigned_to_id == user.id

def _allowed_transitions(role: str, old_status: str) -> set[str]:
    """
    Return allowed next statuses for a given role and current status.

    Jira-style "Transition Map" (single source of truth).
    """
    _all = {k for (k, _) in Task.STATUS_CHOICES}
    _dev_transitions = {
        "todo":        {"in_progress"},
        "in_progress": {"qa", "in_review"},
    }
    _design_transitions = {
        "todo":        {"in_progress"},
        "in_progress": {"in_review"},
    }
    _qa_transitions = {
        "todo":        {"in_progress"},
        "in_progress": {"qa", "in_review"},
        "in_review":   {"done", "in_progress"},
        "qa":          {"done", "in_progress"},
    }
    _deploy_transitions = {
        "in_review": {"done"},
        "qa":        {"done"},
    }

    workflow_map = {
        # ── Platform ──────────────────────────────────────────────────────────
        "admin":            {"*": _all},
        "user":             {},

        # ── Product & Project Management ───────────────────────────────────────
        "project_lead":     {"*": _all},
        "product_manager":  {"*": _all},
        "business_analyst": {
            "todo":        {"in_progress"},
            "in_progress": {"in_review"},
        },

        # ── Design ────────────────────────────────────────────────────────────
        "ux_researcher":  _design_transitions,
        "ui_designer":    _design_transitions,
        "ui_ux_designer": _design_transitions,

        # ── Engineering ───────────────────────────────────────────────────────
        "developer":     _dev_transitions,
        "frontend_dev":  _dev_transitions,
        "backend_dev":   _dev_transitions,
        "fullstack_dev": _dev_transitions,
        "mobile_dev":    _dev_transitions,
        "dba": {
            "todo":        {"in_progress"},
            "in_progress": {"done"},
        },

        # ── QA & Testing ──────────────────────────────────────────────────────
        "tester":          _qa_transitions,
        "qa":              _qa_transitions,
        "qa_manual":       _qa_transitions,
        "qa_automation":   _qa_transitions,
        "security_tester": {
            "in_review": {"done", "in_progress"},
            "qa":        {"done", "in_progress"},
        },

        # ── DevOps & Infrastructure ────────────────────────────────────────────
        "devops_engineer": _deploy_transitions,
        "cloud_architect": _deploy_transitions,
        "deployment_team": _deploy_transitions,

        # ── Data Science & Analytics ───────────────────────────────────────────
        "data_analyst": {
            "todo":        {"in_progress"},
            "in_progress": {"done"},
        },
        "data_scientist": {
            "todo":        {"in_progress"},
            "in_progress": {"in_review"},
            "in_review":   {"done"},
        },

        # ── IT Support & Security ──────────────────────────────────────────────
        "helpdesk": {
            "todo":        {"in_progress"},
            "in_progress": {"done"},
        },
        "ciso": {
            "todo":        {"in_progress"},
            "in_progress": {"in_review"},
            "in_review":   {"done"},
            "qa":          {"done", "in_progress"},
        },

        # ── Delivery ──────────────────────────────────────────────────────────
        "delivery_team": {"*": _all},
    }

    role_map = workflow_map.get(role, {})
    if "*" in role_map:
        return role_map["*"]
    if role_map:
        return role_map.get(old_status, set())

    return set()


def _status_label(status: str) -> str:
    return dict(Task.STATUS_CHOICES).get(status, status.replace("_", " ").title())


def _role_label(role: str) -> str:
    static = dict(Profile.ROLE_CHOICES)
    if role in static:
        return static[role]
    return role.replace("_", " ").title()


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
        # 'done' is the only terminal status — in_review/qa are still in flight.
        completed = status_counts.get('done', 0)
        progress_percent = int((completed / total_tasks) * 100)
    return status_counts, progress_percent


# Map ITTeam.team_type to the ProfileRole / ProjectMembership role keys it owns.
_TEAM_TYPE_TO_ROLES = {
    'initiated':   [],   # catch-all bucket for all tasks (totals only)
    'design':      ['ui_designer', 'ui_ux_designer', 'ux_researcher'],
    'development': ['developer', 'frontend_dev', 'backend_dev',
                    'fullstack_dev', 'mobile_dev', 'dba'],
    'qa':          ['tester', 'qa', 'qa_manual', 'qa_automation', 'security_tester'],
    'devops':      ['devops_engineer', 'cloud_architect', 'deployment_team'],
    'delivery':    ['delivery_team'],
}


def _project_timeline_context(project: Project) -> dict:
    """
    Build Detailed Progress Tracker stages.
    Priority: project-specific stages → global default stages → legacy hardcoded.
    """
    configs = list(
        ProjectStageConfiguration.objects
        .filter(project=project)
        .select_related('team__department')
        .order_by('seq_order')
    )
    if not configs:
        configs = list(
            ProjectStageConfiguration.objects
            .filter(project=None)
            .select_related('team__department')
            .order_by('seq_order')
        )
    if not configs:
        return _legacy_project_timeline_context(project)

    tasks = list(
        Task.objects.filter(project=project).only('id', 'status', 'assigned_to_id')
    )
    membership_role = dict(
        ProjectMembership.objects.filter(project=project).values_list('user_id', 'role')
    )

    # Build role → config_id lookup (first matching wins)
    role_to_cid: dict[str, int] = {}
    for cfg in configs:
        for role in _TEAM_TYPE_TO_ROLES.get(cfg.team.team_type, []):
            role_to_cid.setdefault(role, cfg.pk)

    stage_totals = {cfg.pk: 0 for cfg in configs}
    stage_done   = {cfg.pk: 0 for cfg in configs}

    for t in tasks:
        user_role = membership_role.get(t.assigned_to_id)
        cid = role_to_cid.get(user_role)
        if cid:
            stage_totals[cid] += 1
            if t.status == 'done':
                stage_done[cid] += 1

    # "Initiated" stage absorbs all-task totals
    initiated_cfg = next((c for c in configs if c.team.team_type == 'initiated'), None)
    if initiated_cfg:
        total_all = len(tasks)
        done_all  = sum(1 for t in tasks if t.status == 'done')
        stage_totals[initiated_cfg.pk] = max(stage_totals[initiated_cfg.pk], total_all)
        stage_done[initiated_cfg.pk]   = max(stage_done[initiated_cfg.pk],   done_all)

    stages = []
    active_stage_id = str(configs[0].pk) if configs else None

    for cfg in configs:
        total    = stage_totals[cfg.pk]
        done     = stage_done[cfg.pk]
        complete = bool(total) and done >= total
        stages.append({
            'id':       str(cfg.pk),
            'label':    cfg.get_label(),
            'color':    cfg.team.color,
            'total':    total,
            'done':     done,
            'complete': complete,
            'blocked':  bool(total) and not complete,
        })

    init_sid = str(initiated_cfg.pk) if initiated_cfg else None
    non_init = [s for s in stages if s['id'] != init_sid]

    for s in non_init:
        if s['total'] and not s['complete']:
            active_stage_id = s['id']
            break
    else:
        # All non-init stages are complete (or empty) — pin to last stage with tasks,
        # or fallback to the last stage overall, or the first config.
        stages_with_tasks = [s for s in non_init if s['total'] > 0]
        if stages_with_tasks:
            active_stage_id = stages_with_tasks[-1]['id']
        elif non_init:
            active_stage_id = non_init[-1]['id']
        # else: stays as configs[0] (only an initiated stage exists)

    non_init_with_tasks = [s for s in non_init if s['total'] > 0]
    if not len(tasks):
        health = 'not_started'
    elif non_init_with_tasks and all(s['complete'] for s in non_init_with_tasks):
        health = 'delivered'
    else:
        health = 'on_track'

    return {
        'project_id':      project.id,
        'project_name':    project.name,
        'active_stage_id': active_stage_id,
        'stages':          stages,
        'health':          health,
    }


def _tracker_snapshot(project) -> dict | None:
    """
    Lightweight serialisable version of _project_timeline_context for JSON responses.
    Returns None when there is no project, so callers can safely skip it.
    """
    if not project:
        return None
    ctx = _project_timeline_context(project)
    return {
        'active_stage_id': ctx['active_stage_id'],
        'health': ctx['health'],
        'stages': [
            {
                'id':       s['id'],
                'label':    s['label'],
                'color':    s['color'],
                'total':    s['total'],
                'done':     s['done'],
                'complete': s['complete'],
            }
            for s in ctx['stages']
        ],
    }


def _legacy_project_timeline_context(project: Project) -> dict:
    """Original hardcoded stage logic — used as fallback when no DB config exists."""
    stage_defs = [
        ("initiated",  "Initiated",  "#94a3b8"),
        ("uiux",       "UI/UX",      "#8b5cf6"),
        ("dev",        "Dev",        "#2563eb"),
        ("qa",         "QA/Tester",  "#f59e0b"),
        ("deployment", "Deployment", "#22c55e"),
        ("delivery",   "Delivery",   "#06b6d4"),
    ]
    role_to_stage = {
        "ui_ux_designer":  "uiux",
        "developer":       "dev",
        "frontend_dev":    "dev",
        "backend_dev":     "dev",
        "fullstack_dev":   "dev",
        "mobile_dev":      "dev",
        "tester":          "qa",
        "qa":              "qa",
        "qa_manual":       "qa",
        "qa_automation":   "qa",
        "deployment_team": "deployment",
        "devops_engineer": "deployment",
        "delivery_team":   "delivery",
    }
    ordered = [s[0] for s in stage_defs]
    color_map = {s[0]: s[2] for s in stage_defs}
    label_map = {s[0]: s[1] for s in stage_defs}

    tasks = Task.objects.filter(project=project).only("id", "status", "assigned_to_id")
    membership_role = dict(
        ProjectMembership.objects.filter(project=project).values_list("user_id", "role")
    )

    totals = {sid: 0 for sid in ordered}
    done   = {sid: 0 for sid in ordered}

    for t in tasks:
        sid = role_to_stage.get(membership_role.get(t.assigned_to_id))
        if sid:
            totals[sid] += 1
            if t.status == "done":
                done[sid] += 1

    total_tasks = tasks.count()
    totals["initiated"] = max(totals["initiated"], total_tasks)
    done["initiated"]   = done["initiated"] + tasks.filter(status="done").count()

    stages = []
    active_stage_id = "initiated"
    for sid in ordered:
        total    = totals[sid]
        d        = done[sid]
        complete = bool(total) and d >= total
        stages.append({
            "id": sid, "label": label_map[sid], "color": color_map[sid],
            "total": total, "done": d, "complete": complete,
            "blocked": bool(total) and not complete,
        })

    for s in stages:
        if s["id"] == "initiated":
            continue
        if s["total"] and not s["complete"]:
            active_stage_id = s["id"]
            break
    else:
        if total_tasks:
            active_stage_id = "delivery" if totals.get("delivery") else "initiated"

    non_init = [s for s in stages if s["id"] != "initiated" and s["total"] > 0]
    health = ("not_started" if not any(s["total"] > 0 for s in stages)
              else "delivered" if (non_init and all(s["complete"] for s in non_init))
              else "on_track")

    return {
        "project_id":      project.id,
        "project_name":    project.name,
        "active_stage_id": active_stage_id,
        "stages":          stages,
        "health":          health,
    }


def _task_can_transition(user, task: Task, new_status: str) -> bool:
    """Team-centric visibility, assignee-restricted actions (except admin)."""
    if not _task_can_view(user, task):
        return False

    role = getattr(user, 'profile', None)
    role = role.role if role else 'user'
    if task.project_id and role != 'admin':
        role = ProjectMembership.objects.filter(
            user=user,
            project=task.project,
        ).values_list('role', flat=True).first() or role
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


def _task_can_edit_or_delete(user, task: Task) -> bool:
    """Who can edit a task's own fields or archive (soft-delete) it."""
    if _is_admin_user(user):
        return True
    if task.project_id and _is_project_lead_for_project(user, task.project):
        return True
    return task.created_by_id == user.id


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
    user = request.user
    profile, _ = Profile.objects.get_or_create(user=user)
    global_role = profile.role

    memberships = ProjectMembership.objects.filter(user=user).select_related('project')

    projects_qs = Project.objects.filter(
        id__in=memberships.values_list('project_id', flat=True)
    ).distinct().prefetch_related('members')

    notification_count = _get_notification_count(user)

    # =========================
    # GLOBAL ADMIN
    # =========================
    if global_role == 'admin':
        tasks_qs = Task.objects.all().select_related('project', 'assigned_to')

        activities = list(
            TaskActivity.objects.select_related('user')
            .order_by('-created_at')[:10]
        )

        return {
            'role': 'admin',
            'notification_count': notification_count,
            'tasks_qs': tasks_qs,
            'projects_qs': Project.objects.all(),
            'activities': activities,
            'total_users': Profile.objects.count(),
            'recent_tasks': list(tasks_qs.order_by('-id')[:10]),
            'testing_tasks_qs': Task.objects.none(),
        }

    # =========================
    # PROJECT ROLE BASED
    # =========================

    # ⚠️ safer: collect roles instead of first()
    roles = list(memberships.values_list('role', flat=True))

    if 'project_lead' in roles:
        tasks_qs = Task.objects.filter(
            project__in=projects_qs
        ).select_related('project', 'assigned_to')

    elif 'tester' in roles:
        tasks_qs = Task.objects.filter(
            assigned_to=user
        ).select_related('project', 'assigned_to')

    else:
        tasks_qs = Task.objects.filter(
            assigned_to=user
        ).select_related('project', 'assigned_to')

    activities = list(
        TaskActivity.objects.filter(user=user)
        .order_by('-created_at')[:5]
    )

    return {
        'role': roles[0] if roles else 'user',
        'notification_count': notification_count,
        'tasks_qs': tasks_qs,
        'projects_qs': projects_qs,
        'activities': activities,
        'total_users': None,
        'recent_tasks': list(tasks_qs.order_by('-id')[:10]),
        'testing_tasks_qs': Task.objects.none(),
    }
    
from functools import wraps

def role_required(allowed_roles):
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if _is_admin_user(request.user):
                return view_func(request, *args, **kwargs)

            profile = getattr(request.user, 'profile', None)
            if profile and profile.role in allowed_roles:
                return view_func(request, *args, **kwargs)

            project_role_allowed = ProjectMembership.objects.filter(
                user=request.user,
                role__in=allowed_roles,
            ).exists()
            if not project_role_allowed:
                return redirect('core:home')

            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
    
def home(request):
    if not request.user.is_authenticated:
        return render(request, "core/guest_home.html", _guest_home_context())

    s = _user_workspace_scope(request)

    # ✅ Annotate projects with task counts
    projects = s['projects_qs'].annotate(
        # Count('task'...) joins straight to the table — it bypasses Task's
        # default manager, so archived tasks must be excluded explicitly here.
        task_count=Count('task', filter=Q(task__is_archived=False)),
        done_count=Count('task', filter=Q(task__status='done', task__is_archived=False))
    )

    workspace_projects = []
    for p in projects:
        pct = int(round(100 * p.done_count / p.task_count)) if p.task_count else 0

        workspace_projects.append({
            'project': p,
            'progress': pct,
            'task_count': p.task_count,
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

    role_choices = list(ProjectMembership.ROLE_CHOICES)
    valid_role_values = {value for value, _ in role_choices}
    selected_role = request.GET.get('role', '').strip()
    if selected_role not in valid_role_values:
        selected_role = ''

    visual_tasks_qs = tasks_qs
    if selected_role:
        visual_tasks_qs = visual_tasks_qs.filter(
            assigned_to__projectmembership__role=selected_role,
            assigned_to__projectmembership__project_id=F('project_id'),
        ).distinct()

    today = date.today()
    visual_total = visual_tasks_qs.count()
    visual_completed = visual_tasks_qs.filter(status='done').count()
    visual_active = visual_tasks_qs.filter(status__in=['todo', 'in_progress', 'in_review']).count()
    visual_overdue = visual_tasks_qs.filter(due_date__lt=today).exclude(status='done').count()
    visual_completion_percent = int((visual_completed / visual_total) * 100) if visual_total else 0

    department_progress = []
    for role_value, role_label in role_choices:
        dept_tasks = tasks_qs.filter(
            assigned_to__projectmembership__role=role_value,
            assigned_to__projectmembership__project_id=F('project_id'),
        ).distinct()
        dept_total = dept_tasks.count()
        if not dept_total:
            continue
        dept_done = dept_tasks.filter(status='done').count()
        dept_active = dept_tasks.filter(status__in=['todo', 'in_progress', 'in_review']).count()
        dept_overdue = dept_tasks.filter(due_date__lt=today).exclude(status='done').count()
        department_progress.append({
            'role': role_value,
            'label': role_label,
            'total': dept_total,
            'completed': dept_done,
            'active': dept_active,
            'overdue': dept_overdue,
            'percent': int((dept_done / dept_total) * 100) if dept_total else 0,
        })

    chart_pending = visual_tasks_qs.filter(status='todo').count()
    chart_in_progress = visual_tasks_qs.filter(status='in_progress').count()
    chart_testing = visual_tasks_qs.filter(status='in_review').count()
    chart_done = visual_tasks_qs.filter(status='done').count()

    admin_projects = list(projects_qs[:8]) if role == 'admin' else []
    filter_tasks = list(visual_tasks_qs.order_by('-id')[:25])
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
        'role_choices': role_choices,
        'selected_role': selected_role,
        'visual_total': visual_total,
        'visual_completed': visual_completed,
        'visual_active': visual_active,
        'visual_overdue': visual_overdue,
        'visual_completion_percent': visual_completion_percent,
        'department_progress': department_progress,
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


@login_required(login_url='core:login')
def role_dashboard(request):
    """
    Role-specific dashboards:
    - Admin: user management + org completion
    - Project Lead: command center (quick create + kanban + team load)
    - Member: my tasks list with due countdown + one-click status update
    """
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if _is_admin_user(request.user) or profile.role == "admin":
        today = date.today()

        # ── Project health rows ──────────────────────────────────────
        projects_qs = Project.objects.all().select_related(
            'project_lead', 'created_by'
        ).prefetch_related('members')
        project_rows = []
        for p in projects_qs:
            counts, pct = _project_progress_snapshot(p)
            total = sum(counts.values())
            overdue_count = Task.objects.filter(
                project=p, due_date__lt=today
            ).exclude(status='done').count()
            project_rows.append({
                "id": p.id,
                "name": p.name,
                "department": p.department or "",
                "progress": pct,
                "total": total,
                "todo": counts.get("todo", 0),
                "in_progress": counts.get("in_progress", 0),
                "done_like": counts.get("done", 0),
                "overdue": overdue_count,
                "team_size": p.members.count(),
                "deadline": p.target_end_date,
                "lead": p.project_lead.username if p.project_lead else "—",
                "priority": p.priority or "",
            })

        # ── All active users with role & project count ───────────────
        all_users = (
            User.objects.filter(is_active=True)
            .select_related('profile')
            .annotate(project_count=Count('projectmembership', distinct=True))
            .order_by('username')
        )

        # ── Org-wide overdue tasks ───────────────────────────────────
        overdue_tasks = list(
            Task.objects.filter(due_date__lt=today)
            .exclude(status='done')
            .select_related('project', 'assigned_to')
            .order_by('due_date')[:20]
        )

        # ── Pending invites ─────────────────────────────────────────
        pending_invites_count = ProjectInvite.objects.filter(used=False).count()

        # ── Recent activity ──────────────────────────────────────────
        recent_activity = list(
            TaskActivity.objects.select_related('user', 'task', 'task__project')
            .order_by('-created_at')[:12]
        )

        # ── Totals ───────────────────────────────────────────────────
        total_tasks_count = Task.objects.count()
        total_users_count = User.objects.filter(is_active=True).count()

        return render(request, "core/dashboard_admin_focus.html", {
            "project_rows": project_rows,
            "all_users": all_users,
            "overdue_tasks": overdue_tasks,
            "pending_invites_count": pending_invites_count,
            "recent_activity": recent_activity,
            "total_tasks_count": total_tasks_count,
            "total_users_count": total_users_count,
            "active_projects_count": projects_qs.count(),
            "today": today,
            "notification_count": _get_notification_count(request.user),
        })

    # Project lead detection: either global role or any membership in that role, or Profile flag.
    is_lead = (
        profile.role == "project_lead"
        or profile.isProjectLead
        or ProjectMembership.objects.filter(user=request.user, role="project_lead").exists()
    )

    if is_lead:
        lead_projects = Project.objects.filter(
            Q(project_lead=request.user) | Q(created_by=request.user)
        ).distinct().prefetch_related("members")
        selected_project_id = request.GET.get("project")
        selected_project = None
        if selected_project_id and str(selected_project_id).isdigit():
            selected_project = lead_projects.filter(id=int(selected_project_id)).first()
        if not selected_project:
            selected_project = lead_projects.first()

        board_tasks = []
        status_counts = {"todo": 0, "in_progress": 0, "in_review": 0, "qa": 0, "done": 0}
        progress_percent = 0
        team_load = []
        project_timeline = None
        kanban_columns = [{"status": s, "label": l, "count": 0} for (s, l) in Task.STATUS_CHOICES]
        assignee_choices = []

        if selected_project:
            members = list(selected_project.members.all())
            if selected_project.project_lead and selected_project.project_lead_id not in [u.id for u in members]:
                members.append(selected_project.project_lead)
            assignee_choices = members

            tasks_qs = Task.objects.filter(project=selected_project).select_related("assigned_to")
            status_counts = {
                "todo": tasks_qs.filter(status="todo").count(),
                "in_progress": tasks_qs.filter(status="in_progress").count(),
                "in_review": tasks_qs.filter(status="in_review").count(),
                "qa": tasks_qs.filter(status="qa").count(),
                "done": tasks_qs.filter(status="done").count(),
            }
            kanban_columns = [{"status": s, "label": l, "count": status_counts.get(s, 0)} for (s, l) in Task.STATUS_CHOICES]
            total = sum(status_counts.values())
            # 'done' is the only terminal status — in_review/qa are still in flight.
            completed = status_counts.get("done", 0)
            progress_percent = int((completed / total) * 100) if total else 0

            board_tasks = list(tasks_qs.order_by("-id")[:200])
            for t in board_tasks:
                t.allowed_next_statuses = _task_allowed_next_statuses(request.user, t)
                t.allowed_next_status_labels = [_status_label(s) for s in t.allowed_next_statuses]

            def _task_count_for_roles(roles: list[str]) -> int:
                return Task.objects.filter(
                    project=selected_project,
                    assigned_to__projectmembership__project_id=selected_project.id,
                    assigned_to__projectmembership__role__in=roles,
                ).distinct().count()

            team_load = [
                {"label": "UI/UX", "count": _task_count_for_roles(["ui_ux_designer"]), "color": "#8b5cf6"},
                {"label": "Dev", "count": _task_count_for_roles(["developer"]), "color": "#2563eb"},
                {"label": "QA/Tester", "count": _task_count_for_roles(["tester", "qa"]), "color": "#f59e0b"},
                {"label": "Deployment", "count": _task_count_for_roles(["deployment_team"]), "color": "#22c55e"},
                {"label": "Delivery", "count": _task_count_for_roles(["delivery_team"]), "color": "#06b6d4"},
            ]

            project_timeline = _project_timeline_context(selected_project)

            # ── Overdue & due-soon tasks ─────────────────────────────
            lead_today = date.today()
            overdue_tasks = list(
                Task.objects.filter(
                    project=selected_project,
                    due_date__lt=lead_today,
                ).exclude(status='done')
                .select_related('assigned_to')
                .order_by('due_date')[:15]
            )
            due_soon_tasks = list(
                Task.objects.filter(
                    project=selected_project,
                    due_date__gte=lead_today,
                    due_date__lte=lead_today + timedelta(days=7),
                ).exclude(status='done')
                .select_related('assigned_to')
                .order_by('due_date')[:10]
            )

            # ── Per-member workload breakdown ────────────────────────
            memberships = (
                ProjectMembership.objects.filter(project=selected_project)
                .select_related('user__profile')
            )
            team_breakdown = []
            for pm in memberships:
                m_tasks = Task.objects.filter(
                    project=selected_project, assigned_to=pm.user
                )
                ov = m_tasks.filter(due_date__lt=lead_today).exclude(status='done').count()
                team_breakdown.append({
                    'user': pm.user,
                    'role': pm.role,
                    'role_label': dict(ProjectMembership.ROLE_CHOICES).get(pm.role, pm.role),
                    'todo': m_tasks.filter(status='todo').count(),
                    'in_progress': m_tasks.filter(status='in_progress').count(),
                    'in_review': m_tasks.filter(status__in=['in_review', 'qa']).count(),
                    'done': m_tasks.filter(status='done').count(),
                    'overdue': ov,
                    'total': m_tasks.count(),
                })
            team_breakdown.sort(key=lambda x: -x['total'])

        return render(request, "core/dashboard_lead_command_center.html", {
            "projects": lead_projects,
            "selected_project": selected_project,
            "tasks": board_tasks,
            "status_counts": status_counts,
            "progress_percent": progress_percent,
            "team_load": team_load,
            "project_timeline": project_timeline,
            "kanban_columns": kanban_columns,
            "assignee_choices": assignee_choices,
            "overdue_tasks": overdue_tasks if selected_project else [],
            "due_soon_tasks": due_soon_tasks if selected_project else [],
            "team_breakdown": team_breakdown if selected_project else [],
            "today": date.today(),
            "notification_count": _get_notification_count(request.user),
        })

    # Member view (default): My Tasks.
    my_tasks = (
        Task.objects.filter(assigned_to=request.user)
        .select_related("project", "assigned_to")
        .order_by("due_date", "-id")
    )
    my_tasks_list = list(my_tasks[:80])
    for t in my_tasks_list:
        t.allowed_next_statuses = _task_allowed_next_statuses(request.user, t)
        t.primary_next_status = t.allowed_next_statuses[0] if t.allowed_next_statuses else ""
        t.primary_next_status_label = _status_label(t.primary_next_status) if t.primary_next_status else ""

    return render(request, "core/dashboard_member_my_tasks.html", {
        "tasks": my_tasks_list,
        "today": timezone.now().date(),
    })
    


def post_login_handler(request, user):
    profile, _ = Profile.objects.get_or_create(user=user)

    # ✅ Apply invite if exists
    invite = ProjectInvite.objects.filter(
        email__iexact=user.email,
        used=False
    ).first()

    if invite:
        ProjectMembership.objects.get_or_create(
            user=user,
            project=invite.project,
            defaults={'role': invite.role}
        )
        if invite.team:
            invite.team.members.add(user)

        invite.used = True
        invite.save()

    # ✅ Role-based redirect
    if profile.role == 'admin':
        return redirect('core:dashboard')
    elif ProjectMembership.objects.filter(user=user, role__in=['project_lead', 'delivery_team']).exists():
        return redirect('core:projects')
    else:
        return redirect('core:home')  
    
@login_required(login_url='core:login')
def teams(request):
    profile = getattr(request.user, 'profile', None)
    role = getattr(profile, 'role', 'user')
    # also treat users who are project_lead via membership or isProjectLead flag
    _is_lead = _is_project_lead(request.user)

    my_teams = None

    if role == 'admin':
        projects = Project.objects.prefetch_related('members')
        tasks = Task.objects.select_related('project', 'assigned_to').all()
        members = User.objects.filter(is_active=True)
        can_approve = True
        my_teams = Team.objects.prefetch_related('members', 'project').select_related('project')
    elif role == 'project_lead' or _is_lead:
        # All projects where user is lead or any kind of member
        projects = Project.objects.filter(
            Q(project_lead=request.user) |
            Q(projectmembership__user=request.user)
        ).distinct().prefetch_related('members')
        tasks = Task.objects.filter(
            Q(project__project_lead=request.user) |
            Q(project__projectmembership__user=request.user)
        ).distinct().select_related('project', 'assigned_to')
        members = User.objects.filter(
            projectmembership__project__in=projects
        ).distinct()
        can_approve = True
        my_teams = Team.objects.filter(
            project__in=projects
        ).prefetch_related('members', 'project').select_related('project')
    elif role == 'delivery_team':
        led_teams = _teams_led_by(request.user)
        team_members = _team_scope_users(led_teams)
        project_ids = led_teams.values_list('project_id', flat=True)
        tasks = Task.objects.filter(
            Q(team__in=led_teams) | Q(assigned_to__in=team_members),
            project_id__in=project_ids,
        ).select_related('project', 'assigned_to', 'team').distinct()
        projects = Project.objects.filter(id__in=project_ids).distinct()
        members = team_members
        can_approve = True
        my_teams = led_teams
    else:
        projects = Project.objects.filter(members=request.user).distinct().prefetch_related('members')
        tasks = Task.objects.filter(project__in=projects, assigned_to=request.user).select_related('project', 'assigned_to')
        members = User.objects.filter(id=request.user.id)
        can_approve = False

    status_counts = get_status_counts(tasks)
    project_count = projects.count()
    team_member_count = members.exclude(id=request.user.id).count() if role != 'admin' else members.count()

    return render(request, 'core/teams.html', {
        'projects': projects,
        'tasks': tasks,
        'members': members,
        'status_counts': status_counts,
        'project_count': project_count,
        'team_member_count': team_member_count,
        'role': role if (role in ('admin', 'project_lead', 'delivery_team')) else ('project_lead' if _is_lead else role),
        'can_approve': can_approve,
        'my_teams': my_teams,
        'can_create_team': (role == 'admin' or role == 'project_lead' or _is_lead),
    })
    
@login_required
def update_member_role(request, project_id, user_id):
    project = get_object_or_404(Project, id=project_id)

    if not _can_manage_project_members(request.user, project):
        return HttpResponseForbidden("Not allowed")

    user = get_object_or_404(User, id=user_id)
    new_role = (request.POST.get("role") or "").strip()

    # project_lead is excluded here (and from the role dropdown itself) —
    # that assignment happens through project settings, not this form.
    valid_roles = {c[0] for c in ProjectMembership.ROLE_CHOICES if c[0] != 'project_lead'}

    if new_role in valid_roles:
        ProjectMembership.objects.update_or_create(
            user=user,
            project=project,
            defaults={'role': new_role}
        )
        try:
            NotificationService().notify_project_member_added(project, user, new_role, request.user)
        except Exception:
            pass
        messages.success(request, f"{user.username}'s role updated successfully.")
    else:
        messages.error(request, "Invalid role selected.")

    return redirect('core:project_team', project_id=project.id)

@login_required
def create_team(request, project_id):
    project = get_object_or_404(Project, id=project_id)

    if not _can_manage_project_members(request.user, project):
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

                # =====================================================
                # CREATE TEAM
                # =====================================================

                team = Team.objects.create(
                    name=name,
                    project=project
                )

                # =====================================================
                # SAME DEPARTMENT USERS ONLY
                # =====================================================

                allowed_users = _department_users_for_project(project)

                # =====================================================
                # ASSIGN LEAD
                # =====================================================

                lead = None

                if lead_id:

                    lead = allowed_users.filter(id=lead_id).first()

                    if lead:
                        team.lead = lead

                        # Preserve existing role; only default to 'developer' on first join.
                        # Team-lead distinction comes from Team.lead FK, not membership role.
                        ProjectMembership.objects.get_or_create(
                            user=lead,
                            project=project,
                            defaults={'role': 'developer'}
                        )

                # =====================================================
                # ASSIGN MEMBERS
                # =====================================================

                members = allowed_users.filter(id__in=member_ids)

                if lead:
                    members = members.exclude(id=lead.id)

                team.members.set(members)
                team.save()

                # =====================================================
                # ENSURE PROJECT MEMBERSHIP
                # =====================================================

                for member in members:

                    ProjectMembership.objects.get_or_create(
                        user=member,
                        project=project,
                        defaults={'role': 'developer'}
                    )

                # =====================================================
                # NOTIFY LEAD
                # =====================================================

                if lead:
                    _create_notification(
                        lead,
                        'member_added',
                        f'You are leading team "{team.name}"',
                        f'You were assigned as team lead for "{team.name}" in project "{project.name}".',
                        project=project,
                    )

                # =====================================================
                # BULK NOTIFICATIONS FOR MEMBERS
                # =====================================================

                notifications = []

                for member in members:

                    notifications.append(
                        Notification(
                            user=member,
                            notification_type='member_added',
                            title='Added to Team',
                            message=(
                                f'You were added to team "{team.name}" '
                                f'in project "{project.name}".'
                            ),
                            project=project
                        )
                    )

                if notifications:
                    Notification.objects.bulk_create(notifications)

                # =====================================================
                # OPTIONAL SERVICE NOTIFICATION
                # =====================================================

                try:
                    NotificationService().notify_team_changed(
                        team,
                        request.user,
                        action="created"
                    )
                except Exception:
                    pass

                messages.success(request, "Team created successfully")

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return redirect('core:project_detail', project.id)


@login_required
def edit_team(request, team_id):

    team = get_object_or_404(Team, id=team_id)
    project = team.project

    if not _can_manage_project_members(request.user, project) and request.user != team.lead:
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

                # =====================================================
                # UPDATE TEAM NAME
                # =====================================================

                team.name = name

                # =====================================================
                # SAME DEPARTMENT USERS ONLY
                # =====================================================

                allowed_users = _department_users_for_project(project)

                # =====================================================
                # ASSIGN LEAD
                # =====================================================

                lead = None

                if lead_id:

                    lead = allowed_users.filter(id=lead_id).first()

                    if lead:

                        team.lead = lead

                        # Preserve existing role; team-lead distinction is stored on Team.lead.
                        ProjectMembership.objects.get_or_create(
                            user=lead,
                            project=project,
                            defaults={'role': 'developer'}
                        )

                # =====================================================
                # ASSIGN MEMBERS
                # =====================================================

                members = allowed_users.filter(id__in=member_ids)

                if lead:
                    members = members.exclude(id=lead.id)

                team.members.set(members)
                team.save()

                # =====================================================
                # ENSURE PROJECT MEMBERSHIP
                # =====================================================

                for member in members:

                    ProjectMembership.objects.get_or_create(
                        user=member,
                        project=project,
                        defaults={'role': 'developer'}
                    )

                # =====================================================
                # BULK NOTIFICATIONS
                # =====================================================

                notifications = []

                for member in members:

                    notifications.append(
                        Notification(
                            user=member,
                            notification_type='member_added',
                            title='Team Updated',
                            message=(
                                f'Your team "{team.name}" '
                                f'was updated in project "{project.name}".'
                            ),
                            project=project
                        )
                    )

                if notifications:
                    Notification.objects.bulk_create(notifications)

                # =====================================================
                # OPTIONAL SERVICE NOTIFICATION
                # =====================================================

                try:
                    NotificationService().notify_team_changed(
                        team,
                        request.user,
                        action="updated"
                    )
                except Exception:
                    pass

                messages.success(request, "Team updated successfully")

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return redirect('core:teams')

@login_required
def delete_team(request, team_id):
    team = get_object_or_404(Team, id=team_id)
    project = team.project

    if not _can_manage_project_members(request.user, project):
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
        'qa': tasks.filter(status='qa').count(),
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
    if role == 'admin' or _is_project_lead_for_project(user, project):
        # Admin & Project Lead → all tasks
        tasks = Task.objects.filter(project=project)
    
    elif role == 'delivery_team':
        # Delivery team leads see work for teams they lead in this project.
        led_teams = _teams_led_by(user, project)
        team_users = _team_scope_users(led_teams)
        tasks = Task.objects.filter(
            project=project,
        ).filter(Q(team__in=led_teams) | Q(assigned_to__in=team_users))
          
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
        # 'done' is the only terminal status — in_review/qa are still in flight.
        completed = status_counts.get('done', 0)
        progress_percent = int((completed / total_tasks) * 100)

    board_tasks = list(tasks)
    for task in board_tasks:
        task.allowed_next_statuses = _task_allowed_next_statuses(user, task)
        task.allowed_next_status_labels = [_status_label(status) for status in task.allowed_next_statuses]
        task.can_edit_or_delete = _task_can_edit_or_delete(user, task)

    # Effective role for this project — membership role overrides global profile role
    project_role = ProjectMembership.objects.filter(
        user=user, project=project,
    ).values_list('role', flat=True).first() or role

    context = {
        'project': project,
        'tasks': board_tasks,
        'status_choices': Task.STATUS_CHOICES,
        'status_counts': status_counts,
        'progress_percent': progress_percent,
        'can_manage_members': _can_manage_project_members(request.user, project),
        'invite_role_choices': ProjectMembership.ROLE_CHOICES,
        'workflow_rows': _workflow_transition_rows(),
        'role_label': _role_label(role),
        'user_role': project_role,
        'project_timeline': _project_timeline_context(project),
        'teams': Team.objects.filter(project=project).prefetch_related('members', 'lead'),
        'users': project.members.all(),
        'department_choices': DepartmentPipelineSettings.DEPARTMENT_CHOICES,
        'is_admin': _is_admin_user(request.user),
    }
    return render(request, 'core/project_board.html', context)


@login_required(login_url='core:login')
def project_backlog(request, project_id):
    project = get_object_or_404(Project, id=project_id)
    if not _project_accessible_by(request.user, project):
        return redirect('core:projects')

    qs = Task.objects.select_related('assigned_to').filter(project=project).order_by('status', '-id')
    if request.user.profile.role == 'delivery_team':
        led_teams = _teams_led_by(request.user, project)
        team_users = _team_scope_users(led_teams)
        qs = qs.filter(Q(team__in=led_teams) | Q(assigned_to__in=team_users)).distinct()
    elif not _is_project_lead_for_project(request.user, project):
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
    overdue_tasks = Task.objects.filter(project=project, due_date__lt=date.today()).exclude(status='done').count()
    due_soon_tasks = Task.objects.filter(
        project=project,
        due_date__gte=date.today(),
        due_date__lte=date.today() + timedelta(days=2),
    ).exclude(status='done').count()

    progress_percent = 0
    if total_tasks > 0:
        progress_percent = int((done_tasks / total_tasks) * 100)

    # Get available users for adding to project (not yet members)
    available_users = User.objects.filter(is_active=True).exclude(
        id__in=project.members.all()
    ).exclude(id=project.created_by.id)

    # Repair stale ProjectMembership rows where role='admin' but user is a project lead
    ProjectMembership.objects.filter(
        project=project,
        role='admin',
        user__profile__role='project_lead',
    ).update(role='project_lead')

    # Annotate each member with their project-specific role for correct badge display
    from django.db.models import OuterRef, Subquery
    members_annotated = project.members.annotate(
        project_role=Subquery(
            ProjectMembership.objects.filter(
                user=OuterRef('pk'),
                project=project
            ).values('role')[:1]
        )
    ).select_related('profile').order_by('username')

    # Current user's project-specific role (for banner and permission display)
    user_membership = ProjectMembership.objects.filter(
        user=request.user, project=project
    ).first()
    user_project_role = user_membership.role if user_membership else request.user.profile.role

    can_manage = _can_manage_project_members(request.user, project)

    # Role choices for invite/update — project_lead excluded from dropdown
    # (project lead assignment happens through project settings, not invite flow)
    invite_role_choices = [
        (r, l) for r, l in ProjectMembership.ROLE_CHOICES if r != 'project_lead'
    ]

    context = {
        'project': project,
        'done_tasks': done_tasks,
        'total_tasks': total_tasks,
        'overdue_tasks': overdue_tasks,
        'due_soon_tasks': due_soon_tasks,
        'progress_percent': progress_percent,
        'can_manage_members': can_manage,
        'users': available_users,
        'members_annotated': members_annotated,
        'user_project_role': user_project_role,
        'teams': Team.objects.filter(project=project).prefetch_related('members', 'lead'),
        'invite_role_choices': invite_role_choices,
        'pending_invites': ProjectInvite.objects.filter(project=project, used=False).select_related('team'),
        'role_choices': ProjectMembership.ROLE_CHOICES,
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
        ('qa', 'Send to QA'),
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

    elif role == 'project_lead':
        # ✅ Leads see all project tasks
        tasks = Task.objects.select_related('project', 'assigned_to').filter(
                  Q(project__projectmembership__user=request.user) |
                  Q(project__project_lead=request.user) 
        ).distinct()

        projects = Project.objects.filter(
            Q(projectmembership__user=request.user) |
            Q(project_lead=request.user)
        ).distinct()
        
        users = User.objects.filter(projectmembership__project__in=projects).distinct()

    elif role == 'delivery_team':
        led_teams = _teams_led_by(request.user)
        team_users = _team_scope_users(led_teams)
        project_ids = led_teams.values_list('project_id', flat=True)
        tasks = Task.objects.select_related('project', 'assigned_to', 'team').filter(
            Q(team__in=led_teams) | Q(assigned_to__in=team_users),
            project_id__in=project_ids,
        ).distinct()

        projects = Project.objects.filter(id__in=project_ids).distinct()
        users = team_users

    else:
        tasks = Task.objects.select_related('project', 'assigned_to').filter(
            Q(project__members=request.user) | Q(assigned_to=request.user)
        ).distinct()

        projects = Project.objects.filter(members=request.user)
        users = User.objects.filter(id=request.user.id)

    # ✅ Apply Filters (OUTSIDE role block → applies to all)
    if project_id:
        tasks = tasks.filter(project_id=project_id)

    if status:
        tasks = tasks.filter(status=status)

    # `users` above is already scoped per-role (admins see everyone, leads/
    # delivery-team see their own project members, regular members see only
    # themselves) — the dropdown only ever offers users the requester can
    # already see, so there's no need to additionally restrict this to
    # role == 'admin'. That extra gate was silently dropping the selection
    # for project leads and delivery-team leads even though the User filter
    # was visibly rendered and populated for them.
    if user_id:
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
        'teams': Team.objects.filter(project__in=projects).select_related('project', 'lead').prefetch_related('members'),
        'is_project_lead': _is_project_lead(request.user),
        'status_choices': status_choices,
        'status_counts': status_counts,
        'today': date.today(),
    })
    
@login_required
@role_required(['developer'])
def start_task(request, task_id):
    # Backward-compatible endpoint (use unified transition)
    task = get_object_or_404(Task, id=task_id)
    if not _task_can_transition(request.user, task, 'in_progress'):
        return redirect('core:dashboard')
    old_status = task.status
    task._updated_by = request.user
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
    task._updated_by = request.user
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
    task._updated_by = request.user
    task.status = 'done'
    task.save()
    _log_task_activity(task, request.user, "Status changed", _status_label(old), _status_label('done'))
    return redirect('core:dashboard')

@login_required
def create_task(request):
    assignee_id = request.GET.get('assignee') or request.GET.get('assigned_to')
    selected_project = request.GET.get('project') or request.GET.get('project_id')

    # Check if user is admin OR project lead
    user_profile = getattr(request.user, 'profile', None)
    is_admin = user_profile and user_profile.role == 'admin'
    is_project_lead = _is_project_lead(request.user)

    if is_admin:
        projects = Project.objects.all()
    elif is_project_lead:
        projects = Project.objects.filter(
            Q(project_lead=request.user) |
            Q(projectmembership__user=request.user)
        ).distinct()
    else:
        # Regular members can create tasks for projects they're members of
        projects = Project.objects.filter(
            Q(projectmembership__user=request.user) | Q(project_lead=request.user)
        ).distinct()
    
    # If no projects available, redirect with a role-appropriate message
    if not projects.exists():
        if is_project_lead:
            messages.info(
                request,
                "You don't have any projects yet. Create a project first, then you can add tasks to it."
            )
        else:
            messages.warning(
                request,
                "You need to be a member of a project to create tasks."
            )
        return redirect('core:projects')
    users = User.objects.none()
    selected_assignee = None

    if assignee_id:
        try:
            selected_assignee = User.objects.get(id=assignee_id)
            projects = projects.filter(
                Q(projectmembership__user=selected_assignee) | Q(project_lead=selected_assignee)
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
    teams_qs = Team.objects.filter(project__in=projects).select_related('project', 'lead').prefetch_related('members')

    if request.method == 'POST':
        title = request.POST.get('title')
        description = request.POST.get('description')
        assigned_to_id = request.POST.get('assigned_to')
        project_id = request.POST.get('project')
        team_id = request.POST.get('team')
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
        team = None
        if team_id:
            team = Team.objects.filter(id=team_id, project=project).first()
            if not team:
                messages.error(request, "Invalid team selected")
                return redirect(f'/tasks/create/?project={project_id}')

        # 🔐 CORE SECURITY (JIRA RULE)
        if assigned_user not in project.members.all() and assigned_user != project.project_lead:
            messages.error(request, "User not part of this project")
            return redirect(f'/tasks/create/?project={project_id}')

        if team and assigned_user != team.lead and not team.members.filter(id=assigned_user.id).exists():
            messages.error(request, "Assigned user is not part of the selected team")
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
            team=team,
        )

        # Labels (optional)
        cleaned_ids = [int(x) for x in label_ids if str(x).isdigit()]
        if cleaned_ids:
            task.labels.set(Label.objects.filter(id__in=cleaned_ids))

        for uploaded_file in request.FILES.getlist('attachments') + request.FILES.getlist('file'):
            attachment = TaskAttachment.objects.create(
                task=task,
                file=uploaded_file,
                uploaded_by=request.user,
            )
            _log_task_activity(task, request.user, "File uploaded", new_value=attachment.file.name.split('/')[-1])

        _log_task_activity(task, request.user, "Task created", new_value=task.issue_key)
        try:
            NotificationService().notify_task_created(task, request.user)
        except Exception:
            pass

        if task.due_date and task.status != 'done':
            from .notifications import NotificationService
            notifier = NotificationService()
            if task.due_date < date.today():
                notifier.notify_overdue_task(task)
            elif task.due_date <= date.today() + timedelta(days=1):
                notifier.notify_due_date_approaching(task, days_before=(task.due_date - date.today()).days)

        messages.success(request, "Task created successfully")
        return redirect('core:tasks')

    return render(request, 'core/create_task.html', {
        'projects': projects,
        'users': users,
        'selected_project': selected_project,
        'selected_assignee': selected_assignee.id if selected_assignee else '',
        'labels': labels_qs,
        'teams': teams_qs,
    })


@login_required
def edit_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)

    if not _task_can_view(request.user, task):
        return redirect('core:tasks')
    if not _task_can_edit_or_delete(request.user, task):
        messages.error(request, "You don't have permission to edit this task.")
        return redirect('core:task_detail', task_id=task.id)

    project = task.project
    users = project.members.all() if project else User.objects.none()
    if project and project.project_lead and project.project_lead not in users:
        users = users | User.objects.filter(id=project.project_lead.id)

    if request.method == 'POST':
        title = (request.POST.get('title') or '').strip()
        description = request.POST.get('description', '')
        priority = request.POST.get('priority') or task.priority
        assigned_to_id = request.POST.get('assigned_to')
        due_date_raw = (request.POST.get('due_date') or '').strip()
        start_date_raw = (request.POST.get('start_date') or '').strip()
        delivery_date_raw = (request.POST.get('delivery_date') or '').strip()

        if not title:
            messages.error(request, "Title is required")
            return redirect('core:edit_task', task_id=task.id)

        assigned_user = task.assigned_to
        if assigned_to_id:
            assigned_user = get_object_or_404(User, id=assigned_to_id)
            # Same JIRA-style rule create_task enforces.
            if project and assigned_user not in project.members.all() and assigned_user != project.project_lead:
                messages.error(request, "User not part of this project")
                return redirect('core:edit_task', task_id=task.id)

        def _parse_date(raw):
            if not raw:
                return None
            y, m, d = [int(x) for x in raw.split("-")]
            return date(y, m, d)

        try:
            due_dt = _parse_date(due_date_raw)
        except Exception:
            messages.error(request, "Invalid due date")
            return redirect('core:edit_task', task_id=task.id)
        try:
            start_dt = _parse_date(start_date_raw)
        except Exception:
            start_dt = task.start_date
        try:
            delivery_dt = _parse_date(delivery_date_raw)
        except Exception:
            delivery_dt = task.delivery_date

        # Diff against current values so the activity log only records what
        # actually changed (same convention as update_task_due_date/labels).
        if task.title != title:
            _log_task_activity(task, request.user, "Title changed", task.title, title)
            task.title = title
        if task.description != description:
            _log_task_activity(task, request.user, "Description updated")
            task.description = description
        if task.priority != priority:
            _log_task_activity(task, request.user, "Priority changed", task.priority, priority)
            task.priority = priority
        if task.assigned_to_id != (assigned_user.id if assigned_user else None):
            _log_task_activity(
                task, request.user, "Assigned changed",
                getattr(task.assigned_to, 'username', 'Unassigned'),
                getattr(assigned_user, 'username', 'Unassigned'),
            )
            task.assigned_to = assigned_user
        if task.due_date != due_dt:
            _log_task_activity(
                task, request.user, "Due date changed",
                task.due_date.isoformat() if task.due_date else "",
                due_dt.isoformat() if due_dt else "",
            )
            task.due_date = due_dt
        task.start_date = start_dt
        task.delivery_date = delivery_dt

        task._updated_by = request.user
        task.save()

        messages.success(request, "Task updated successfully")
        return redirect('core:task_detail', task_id=task.id)

    return render(request, 'core/edit_task.html', {
        'task': task,
        'users': users,
    })


@login_required
def archive_task(request, task_id):
    """Soft-delete: hides the task everywhere instead of removing it, so its
    comments/attachments/activity history survive (Task.all_objects can still
    reach it, e.g. for restore_task)."""
    if request.method != "POST":
        return redirect('core:tasks')

    task = get_object_or_404(Task, id=task_id)

    if not _task_can_edit_or_delete(request.user, task):
        messages.error(request, "You don't have permission to delete this task.")
        return redirect('core:task_detail', task_id=task.id)

    task.is_archived = True
    task.save(update_fields=["is_archived"])
    _log_task_activity(task, request.user, "Task deleted", new_value=task.issue_key)

    messages.success(request, f"Task '{task.title}' deleted.")
    return redirect('core:tasks')


@login_required
def restore_task(request, task_id):
    """Undo archive_task. Admin/project-lead only — a stricter bar than
    delete, since restoring can resurrect a task into someone else's board."""
    if request.method != "POST":
        return redirect('core:tasks')

    task = get_object_or_404(Task.all_objects, id=task_id)

    is_lead = task.project_id and _is_project_lead_for_project(request.user, task.project)
    if not (_is_admin_user(request.user) or is_lead):
        messages.error(request, "You don't have permission to restore this task.")
        return redirect('core:tasks')

    task.is_archived = False
    task.save(update_fields=["is_archived"])
    _log_task_activity(task, request.user, "Task restored", new_value=task.issue_key)

    messages.success(request, f"Task '{task.title}' restored.")
    return redirect('core:task_detail', task_id=task.id)


@login_required
def update_task_status(request, task_id, new_status):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)

    task = get_object_or_404(Task, id=task_id)

    # 🔐 Permission checks (visibility + workflow)
    if not _task_can_transition(request.user, task, new_status):
        return JsonResponse({'status': 'error', 'message': 'Unauthorized transition'}, status=403)

    # ⛔ No change
    if task.status == new_status:
        project_counts, progress_percent = _project_progress_snapshot(task.project)
        return JsonResponse({
            'status': 'no_change',
            'notification_count': _get_notification_count(request.user),
            'status_counts': project_counts,
            'progress_percent': progress_percent,
            'allowed_next_statuses': _task_allowed_next_statuses(request.user, task),
            'tracker': _tracker_snapshot(task.project),
        })

    try:
        with transaction.atomic():
            old_status = task.status

            # 🔥 AUTO-ASSIGN TESTER WHEN MOVED TO IN_REVIEW
            assigned_old = task.assigned_to

            if new_status == 'in_review':
                testers = User.objects.filter(
                    projectmembership__project=task.project,
                    projectmembership__role='tester',
                ).distinct()

                if testers.exists():
                    # 👉 simple: assign first tester
                    task.assigned_to = testers.first()
                else:
                    # fallback
                    task.assigned_to = task.project.project_lead

            # 🔥 RESTRICT DONE → ONLY TESTER
            request_project_role = ProjectMembership.objects.filter(
                user=request.user,
                project=task.project,
            ).values_list('role', flat=True).first()
            if new_status == 'done' and request.user.profile.role != 'admin' \
                    and request_project_role not in ('tester', 'qa'):
                return JsonResponse({
                    'status': 'error',
                    'message': 'Only a tester or QA member can mark a task as Done'
                }, status=403)

            # ✅ Update status
            task._updated_by = request.user
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

            # 📧 Email + in-app workflow notifications
            try:
                notifier = NotificationService()
                notifier.notify_status_change(
                    task=task,
                    old_status=old_status,
                    new_status=new_status,
                    updated_by=request.user,
                )
            except Exception:
                # Email failures shouldn't block the UX.
                pass

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
            'tracker': _tracker_snapshot(task.project),
        })

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=500)


@login_required
def api_transition_task(request, task_id):
    """
    One-click status update endpoint used by member dashboards.
    Payload: { "new_status": "<status>" }
    """
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    task = get_object_or_404(Task, id=task_id)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        body = {}

    new_status = (body.get("new_status") or "").strip()
    if not new_status:
        return JsonResponse({"error": "new_status required"}, status=400)

    if not _task_can_transition(request.user, task, new_status):
        return JsonResponse({"error": "Unauthorized transition"}, status=403)

    old_status = task.status
    if old_status == new_status:
        return JsonResponse({
            "status": "no_change",
            "old": old_status,
            "new": new_status,
            "allowed_next_statuses": _task_allowed_next_statuses(request.user, task),
        })

    with transaction.atomic():
        assigned_old = task.assigned_to

        # Keep existing behavior: auto-assign tester on in_review.
        if new_status == 'in_review' and task.project_id:
            testers = User.objects.filter(
                projectmembership__project=task.project,
                projectmembership__role__in=['tester', 'qa'],
            ).distinct()
            if testers.exists():
                task.assigned_to = testers.first()
            else:
                task.assigned_to = task.project.project_lead

        task._updated_by = request.user
        task.status = new_status
        task.save(update_fields=["status", "assigned_to"])

        _log_task_activity(
            task=task,
            user=request.user,
            action="Status changed",
            old_value=_status_label(old_status),
            new_value=_status_label(new_status),
        )

        if assigned_old != task.assigned_to:
            _log_task_activity(
                task=task,
                user=request.user,
                action="Assigned changed",
                old_value=getattr(assigned_old, 'username', 'Unassigned'),
                new_value=getattr(task.assigned_to, 'username', 'Unassigned'),
            )

        try:
            notifier = NotificationService()
            notifier.notify_status_change(task, old_status, new_status, request.user)
        except Exception:
            pass

    return JsonResponse({
        "status": "success",
        "old": old_status,
        "new": new_status,
        "assigned_to": task.assigned_to.username if task.assigned_to else None,
        "allowed_next_statuses": _task_allowed_next_statuses(request.user, task),
    })

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
        'can_edit_or_delete': _task_can_edit_or_delete(request.user, task),
    })



@login_required
@role_required(['tester'])
def reject_task(request, task_id):
    task = get_object_or_404(Task, id=task_id)
    # Backward-compatible endpoint (use unified transition)
    if not _task_can_transition(request.user, task, 'in_progress'):
        return redirect('core:dashboard')
    old = task.status
    task._updated_by = request.user
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
            # Raw ISO (UTC, tz-aware) — the browser converts to the viewer's
            # own local time when it formats this for display. Pre-formatting
            # a fixed "%d %b %H:%M" string server-side bakes in UTC with no
            # offset info, so it silently reads as local time and is wrong
            # for anyone not in UTC.
            "time": c.created.isoformat()
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
    try:
        from django.template.loader import render_to_string
        svc = NotificationService()
        recipients = set()
        if task.assigned_to:
            recipients.add(task.assigned_to)
        if task.project:
            recipients.update(svc.get_project_leads(task.project))
        if task.team and task.team.lead:
            recipients.add(task.team.lead)

        commenter_name = request.user.get_full_name() or request.user.username
        comment_preview = text[:120] + ('…' if len(text) > 120 else '')
        task_url = svc.get_task_url(task)

        for recipient in recipients:
            if recipient == request.user:
                continue

            title   = f"New comment on {task.issue_key}"
            message = f"{commenter_name} commented: \"{comment_preview}\""

            Notification.objects.create(
                user=recipient,
                notification_type='task_commented',
                title=title,
                message=message,
                task=task,
                project=task.project,
            )

            if recipient.email:
                context = {
                    'recipient':    recipient,
                    'task':         task,
                    'commenter':    request.user,
                    'comment_text': text,
                    'task_url':     task_url,
                }
                html_content = render_to_string('emails/comment_added.html', context)
                svc.send_email(
                    recipient=recipient,
                    subject=f"New comment on {task.issue_key} — {task.title}",
                    html_content=html_content,
                )
    except Exception:
        pass

    return JsonResponse({
        "status": "ok",
        "comment": {
            "user": comment.user.username,
            "text": comment.text,
            "time": comment.created.isoformat(),
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
    try:
        NotificationService().notify_file_uploaded(attachment, request.user)
    except Exception:
        pass

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
            "time": a.created_at.isoformat()
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

    from .notifications import NotificationService
    notifier = NotificationService()
    if new_due < date.today() and task.status != 'done':
        notifier.notify_overdue_task(task)
    elif new_due <= date.today() + timedelta(days=1) and task.status != 'done':
        notifier.notify_due_date_approaching(task, days_before=(new_due - date.today()).days)

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
    
    
def _client_ip(request) -> str:
    """Best-effort real client IP.

    Render (our only deployment target) terminates connections at its own
    edge and appends the true client IP as the LAST entry of
    X-Forwarded-For. Earlier entries in that header can be set by the
    client itself, so they must not be trusted for rate limiting.
    """
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[-1].strip()
    return request.META.get('REMOTE_ADDR', '')


def _check_login_attempts(request) -> bool:
    """Rate limiting: max 5 attempts per 15 minutes per client IP.

    Tracked in the cache rather than the session, since an anonymous
    session is just a client-supplied cookie — a scripted attacker can
    drop it between requests and get a fresh counter every time.
    """
    key = f'login_attempts_{_client_ip(request)}'
    return cache.get(key, 0) >= 5


def _increment_login_attempts(request):
    """Increment login attempt counter"""
    key = f'login_attempts_{_client_ip(request)}'
    cache.set(key, cache.get(key, 0) + 1, timeout=900)  # 15 minutes


def _clear_login_attempts(request):
    """Reset the counter for this client IP (called on successful login)."""
    cache.delete(f'login_attempts_{_client_ip(request)}')

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

            if not profile.email_verified:
                # Correct password, just not proven ownership of the inbox
                # yet — not a guessing signal, so don't count it as a failed
                # attempt.
                messages.error(
                    request,
                    "Please verify your email before signing in. Check your inbox for the "
                    "confirmation link, or use \"Resend verification email\" below."
                )
                return render(request, "core/auth.html", {"initialTab": "login"})

            # Clear attempt counter on success
            _clear_login_attempts(request)

            login(request, user)
            messages.success(request, f"Welcome back, {user.username}!")

            # Role-based redirect (Jira-style)
            if _is_admin_user(user) or profile.role == 'admin':
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
def register_view(request):
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip().lower()
        password = (request.POST.get("password") or "").strip()
        confirm_password = (request.POST.get("confirm_password") or "").strip()

        # Validation
        if not all([username, email, password]):
            messages.error(request, "All fields are required.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        if len(username) < 3:
            messages.error(request, "Username must be at least 3 characters.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        if len(password) < 8:
            messages.error(request, "Password must be at least 8 characters.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        if User.objects.filter(username__iexact=username).exists():
            messages.error(request, "Username already exists.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        if User.objects.filter(email__iexact=email).exists():
            messages.error(request, "Email already registered.")
            return render(request, "core/auth.html", {"initialTab": "register"})

        try:
            user = User.objects.create_user(username=username, email=email, password=password)
            user.save()

            # default role
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = 'user'
            profile.save()

            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            messages.success(request, f"Welcome to VetriFlow, {username}! Your account has been created.")
            return redirect('core:home')
        except Exception:
            messages.error(request, "An error occurred during registration. Please try again.")

    return render(request, "core/auth.html", {"initialTab": "register"})

def logout_view(request):
    logout(request)
    messages.success(request, "Logged out successfully")
    return redirect('core:home')


@login_required(login_url='core:login')
def role_redirect(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)

    if _is_admin_user(request.user) or profile.role == 'admin':
        return redirect('core:dashboard')

    elif profile.role in ['project_lead', 'team_lead']:
        return redirect('core:projects')

    else:
        return redirect('core:home')


@login_required(login_url='core:login')
def mobile_auth_complete(request):
    """Landing point for the Capacitor app's in-app browser tab after Google OAuth.

    Google blocks OAuth inside an embedded WebView, so the mobile app opens login in a
    separate Custom Tab. Django's redirect() rejects non-http(s) schemes, and a raw
    Location header to a custom scheme isn't reliably picked up by Custom Tabs anyway,
    so this page navigates via JS instead. Android/Capacitor intercepts that navigation
    to close the tab and hand control back to the WebView (see AndroidManifest.xml
    intent-filter and base.html's appUrlOpen listener).

    The Custom Tab (Chrome) and the app's own WebView have separate cookie jars, so the
    session established here doesn't carry over on its own. A short-lived signed token
    is embedded in the deep link instead, which mobile_session_handoff exchanges for a
    real session cookie inside the WebView.
    """
    signer = TimestampSigner()
    token = signer.sign(request.user.pk)
    return render(request, 'core/mobile_auth_complete.html', {'token': token})


def mobile_session_handoff(request):
    """Exchanges the short-lived token from mobile_auth_complete for a real session.

    This runs as a normal navigation inside the app's own WebView (not the Custom Tab),
    so the session cookie it sets actually lands in the WebView's cookie jar.
    """
    token = request.GET.get('token', '')
    signer = TimestampSigner()
    try:
        user_id = signer.unsign(token, max_age=60)
        user = User.objects.get(pk=user_id)
    except (BadSignature, SignatureExpired, User.DoesNotExist):
        return redirect('core:login')

    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    return redirect('core:role_redirect')


# ✅ PASSWORD RESET REQUEST (Jira-style)
def password_reset_request(request):
    if request.method == "POST":
        from django.urls import reverse

        email = (request.POST.get("email") or "").strip().lower()
        try:
            validate_email(email)
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if user:
                # Sign the user id together with their current password hash so the
                # link stops working the moment it's used (or the password changes
                # some other way) — no extra "used" table needed. signing_dumps
                # base64-urlsafe-encodes the payload, so the token is always a
                # single clean URL path segment (raw password hashes can contain
                # '/', which a bare TimestampSigner.sign() would leak verbatim).
                token = signing_dumps(
                    {"uid": user.pk, "pwd": user.password},
                    salt='password-reset',
                )
                reset_url = request.build_absolute_uri(
                    reverse('core:password_reset_confirm', args=[token])
                )
                try:
                    html_content = render_to_string("emails/password_reset.html", {
                        "user": user,
                        "reset_url": reset_url,
                    })
                    text_content = strip_tags(html_content)
                    email_msg = EmailMultiAlternatives(
                        subject="Reset your VetriFlow password",
                        body=text_content,
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        to=[user.email],
                    )
                    email_msg.attach_alternative(html_content, "text/html")
                    email_msg.send()
                except Exception as _email_exc:
                    import traceback
                    print(f"[TF-EMAIL-RESET] FAILED to {user.email}: {_email_exc}")
                    traceback.print_exc()

            # Same message whether or not the account exists — don't reveal it.
            messages.success(
                request,
                "If an account exists with that email, you will receive reset instructions."
            )
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
    return render(request, "core/auth.html", {"initialTab": "login"})


def password_reset_confirm(request, token):
    """Consume the signed link from password_reset_request and set a new password."""
    user = None
    try:
        payload = signing_loads(token, salt='password-reset', max_age=3600)  # 1 hour
        candidate = User.objects.filter(pk=payload.get("uid"), is_active=True).first()
        if candidate and candidate.password == payload.get("pwd"):
            user = candidate
    except (BadSignature, SignatureExpired):
        user = None

    if not user:
        messages.error(request, "This password reset link is invalid or has expired.")
        return redirect('core:password-reset')

    if request.method == "POST":
        password = request.POST.get("password") or ""
        confirm_password = request.POST.get("confirm_password") or ""

        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, "core/password_reset_confirm.html", {"token": token})

        try:
            validate_password(password, user=user)
        except ValidationError as e:
            for err in e.messages:
                messages.error(request, err)
            return render(request, "core/password_reset_confirm.html", {"token": token})

        user.set_password(password)
        user.save(update_fields=["password"])
        messages.success(request, "Your password has been reset. Please sign in.")
        return redirect('core:login')

    return render(request, "core/password_reset_confirm.html", {"token": token})


def _send_verification_email(request, user):
    """Email a signed confirmation link for a newly created (or re-pointed)
    manual account. Must never raise — a delivery failure shouldn't break
    the account-creation/update flow that calls this.

    The email is bound into the signed payload (not just the user id) so a
    stale link can't be replayed to verify a *different* address if the
    email is changed again before the original link is used or expires.
    """
    try:
        token = signing_dumps(
            {"uid": user.pk, "email": user.email},
            salt='email-verification',
        )
        verify_url = request.build_absolute_uri(
            reverse('core:verify_email_confirm', args=[token])
        )
        html_content = render_to_string("emails/verify_email.html", {
            "user": user,
            "verify_url": verify_url,
        })
        text_content = strip_tags(html_content)
        email_msg = EmailMultiAlternatives(
            subject="Verify your VetriFlow email address",
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        email_msg.attach_alternative(html_content, "text/html")
        email_msg.send()
    except Exception as _email_exc:
        import traceback
        print(f"[TF-EMAIL-VERIFY] FAILED to {user.email}: {_email_exc}")
        traceback.print_exc()


def verify_email_confirm(request, token):
    """Consume the signed link from _send_verification_email."""
    user = None
    try:
        payload = signing_loads(token, salt='email-verification', max_age=259200)  # 3 days
        candidate = User.objects.filter(pk=payload.get("uid"), is_active=True).first()
        if candidate and candidate.email.lower() == (payload.get("email") or "").lower():
            user = candidate
    except (BadSignature, SignatureExpired):
        user = None

    if not user:
        messages.error(request, "This verification link is invalid or has expired.")
        return redirect('core:login')

    profile, _ = Profile.objects.get_or_create(user=user)
    profile.email_verified = True
    profile.save(update_fields=["email_verified"])

    messages.success(request, "Your email has been verified. You can now sign in.")
    return redirect('core:login')


def resend_verification_email(request):
    if request.method == "POST":
        email = (request.POST.get("email") or "").strip().lower()
        try:
            validate_email(email)
            user = User.objects.filter(
                email__iexact=email,
                is_active=True,
                profile__email_verified=False,
            ).exclude(profile__oauth_provider__iexact='google').first()
            if user:
                _send_verification_email(request, user)
            # Same message either way — don't reveal whether the account
            # exists or was already verified.
            messages.success(
                request,
                "If that account needs verification, we've sent a new confirmation link."
            )
        except ValidationError:
            messages.error(request, "Please enter a valid email address.")
    return render(request, "core/auth.html", {"initialTab": "login"})


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

        # Search by issue key prefix (e.g., "TF-1" or "TF").
        # issue_key is a Python property, so it cannot be used in ORM filters.
        tasks_by_key = [
            task for task in base_tasks.select_related('project')[:200]
            if query.lower() in task.issue_key.lower()
        ]
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


def _generate_project_key_prefix(name: str) -> str:
    words = [word for word in (name or '').replace('-', ' ').replace('_', ' ').split() if word]
    if len(words) > 1:
        prefix = ''.join(word[0] for word in words)
    else:
        prefix = ''.join(ch for ch in (name or '') if ch.isalnum())[:3]
    return (prefix or 'TF').upper()[:10]



@login_required
@role_required(['admin', 'project_lead'])
def create_project(request):
    google_users = list(
        User.objects.filter(profile__oauth_provider__iexact='google')
        .order_by('username')
    )
    other_users = list(
        User.objects.exclude(profile__oauth_provider__iexact='google')
        .order_by('username')
    )
    users = google_users + other_users
    default_start_date = date.today().isoformat()

    if request.method == 'POST':
        wants_json = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        try:
            with transaction.atomic():
                name = request.POST.get('name', '').strip()
                description = request.POST.get('description', '').strip()
                project_type = request.POST.get('project_type') or 'kanban'
                raw_key_prefix = (request.POST.get('key_prefix') or '').strip()
                key_prefix = (raw_key_prefix or _generate_project_key_prefix(name)).strip().upper()[:10] or 'TF'
                project_lead_id = request.POST.get('project_lead')

                # ====== VALIDATION ======
                if not name:
                    if wants_json:
                        return JsonResponse({'status': 'error', 'error': 'Project name is required'}, status=400)
                    messages.error(request, 'Project name is required')
                    return render(request, 'core/create_project.html', {'users': users, 'default_start_date': default_start_date})

                # Task issue keys (e.g. "TF-1") are only unambiguous if key_prefix is
                # unique — two projects sharing a prefix would both display tasks as
                # "TF-1", "TF-2"... making an old task in one project look like it
                # belongs to (or was auto-created in) the other.
                if Project.objects.filter(key_prefix__iexact=key_prefix).exists():
                    if raw_key_prefix:
                        # User explicitly chose this prefix — make them pick another.
                        error_msg = f'Key prefix "{key_prefix}" is already in use by another project. Please choose a different one.'
                        if wants_json:
                            return JsonResponse({'status': 'error', 'error': error_msg}, status=400)
                        messages.error(request, error_msg)
                        return render(request, 'core/create_project.html', {'users': users, 'default_start_date': default_start_date})
                    # Auto-generated prefix collided — disambiguate quietly.
                    base_prefix = key_prefix[:9]
                    suffix = 2
                    while Project.objects.filter(key_prefix__iexact=f"{base_prefix}{suffix}").exists():
                        suffix += 1
                    key_prefix = f"{base_prefix}{suffix}"

                # ====== NEW FIELDS ======
                category = request.POST.get('category') or 'other'
                project_url = request.POST.get('project_url') or ''
                priority = request.POST.get('priority') or 'p3'
                is_private = request.POST.get('is_private') == 'on'
                default_assignee_id = request.POST.get('default_assignee')

                # ====== PARSE DATES ======
                start_date = None
                target_end_date = None
                start_date_str = request.POST.get('start_date', '').strip()
                target_end_date_str = request.POST.get('target_end_date', '').strip()
                
                if start_date_str:
                    try:
                        from datetime import datetime
                        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        if wants_json:
                            return JsonResponse({'status': 'error', 'error': 'Invalid start date format'}, status=400)
                        messages.warning(request, 'Invalid start date format')
                
                if target_end_date_str:
                    try:
                        from datetime import datetime
                        target_end_date = datetime.strptime(target_end_date_str, '%Y-%m-%d').date()
                    except ValueError:
                        if wants_json:
                            return JsonResponse({'status': 'error', 'error': 'Invalid due date format'}, status=400)
                        messages.warning(request, 'Invalid target date format')

                if start_date and target_end_date and target_end_date < start_date:
                    if wants_json:
                        return JsonResponse({'status': 'error', 'error': 'Due date cannot be before start date'}, status=400)
                    messages.error(request, 'Due date cannot be before start date')
                    return render(request, 'core/create_project.html', {'users': users, 'default_start_date': default_start_date})

                project_lead = User.objects.filter(id=project_lead_id).first() if project_lead_id else None
                default_assignee = User.objects.filter(id=default_assignee_id).first() if default_assignee_id else None
                
                # Handle avatar upload
                avatar = request.FILES.get('avatar')

                project = Project.objects.create(
                    name=name,
                    description=description,
                    created_by=request.user,
                    project_type=project_type,
                    key_prefix=key_prefix,
                    project_lead=project_lead,
                    category=category,
                    project_url=project_url,
                    start_date=start_date,
                    target_end_date=target_end_date,
                    priority=priority,
                    is_private=is_private,
                    default_assignee=default_assignee,
                    avatar=avatar if avatar else None
                )

                # Always add the creator to members so the project appears in
                # their list (projects view filters by members=request.user).
                project.members.add(request.user)
                ProjectMembership.objects.update_or_create(
                    user=request.user,
                    project=project,
                    defaults={'role': 'project_lead'},
                )

                # If a different user was chosen as project lead, add them too.
                if project_lead and project_lead != request.user:
                    project.members.add(project_lead)
                    ProjectMembership.objects.update_or_create(
                        user=project_lead,
                        project=project,
                        defaults={'role': 'project_lead'}
                    )

                messages.success(request, f"Project '{name}' created successfully!")
                if wants_json:
                    return JsonResponse({
                        'status': 'ok',
                        'project': {
                            'id': project.id,
                            'name': project.name,
                            'description': project.description or 'No description',
                            'key_prefix': project.key_prefix,
                            'lead': project.project_lead.username if project.project_lead else '',
                            'start_date': project.start_date.isoformat() if project.start_date else '',
                            'target_end_date': project.target_end_date.isoformat() if project.target_end_date else '',
                            'priority': project.priority,
                        }
                    })
                return redirect('core:projects')
        
        except Exception as e:
            if wants_json:
                return JsonResponse({'status': 'error', 'error': f"Error creating project: {str(e)}"}, status=400)
            messages.error(request, f"Error creating project: {str(e)}")
            return render(request, 'core/create_project.html', {'users': users, 'default_start_date': default_start_date})

    return render(request, 'core/create_project.html', {'users': users, 'default_start_date': default_start_date})


@login_required
def projects(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    global_role = profile.role

    if request.method == "POST":
        messages.info(request, "Project updates via POST are not supported here.")
        return redirect('core:projects')

    if global_role == 'admin':
        projects_qs = Project.objects.prefetch_related('members').annotate(
            total_tasks=Count('task', filter=Q(task__is_archived=False), distinct=True),
            done_tasks=Count('task', filter=Q(task__status='done', task__is_archived=False), distinct=True),
        )
        google_users = list(
            User.objects.filter(profile__oauth_provider__iexact='google')
            .order_by('username')
        )
        other_users = list(
            User.objects.exclude(profile__oauth_provider__iexact='google')
            .order_by('username')
        )
        users = google_users + other_users
    else:
        projects_qs = Project.objects.filter(
            Q(members=request.user) |
            Q(project_lead=request.user) |
            Q(created_by=request.user)
        ).distinct().prefetch_related('members').annotate(
            total_tasks=Count('task', filter=Q(task__is_archived=False), distinct=True),
            done_tasks=Count('task', filter=Q(task__status='done', task__is_archived=False), distinct=True),
        )
        users = User.objects.filter(is_active=True).order_by('username')

    today = date.today()
    project_list = list(projects_qs)
    for p in project_list:
        p.progress_pct = int(p.done_tasks / p.total_tasks * 100) if p.total_tasks else 0
        p.is_overdue = bool(p.target_end_date and p.target_end_date < today)

    return render(request, 'core/projects.html', {
        'projects': project_list,
        'users': users,
        'is_project_lead': _is_project_lead(request.user),
        'default_start_date': today.isoformat(),
        'today': today,
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

    valid_roles = {c[0] for c in ProjectMembership.ROLE_CHOICES}

    # ❌ Block admin assignment
    if role == 'admin':
        return JsonResponse({'error': 'Cannot assign admin role'}, status=400)

    if role not in valid_roles:
        role = 'developer'

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
                        try:
                            NotificationService().notify_project_member_added(project, user, role, request.user, team)
                            NotificationService().notify_team_changed(team, request.user, action="updated")
                        except Exception:
                            pass

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

                # 🔹 Case 2: Existing user not yet in project → send invite email
                # Fall through to the invite creation flow below so they must accept.

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
                team=team,
                invited_by=request.user,
            )

            if project.project_lead and project.project_lead != request.user:
                _create_notification(
                    project.project_lead,
                    'member_added',
                    f"Invite sent: {project.name}",
                    f"{request.user.username} invited {email} as {role}.",
                    project=project,
                )

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)

    # ── Email is sent OUTSIDE the transaction so a delivery failure
    #    never rolls back the saved invite record.
    accept_url = request.build_absolute_uri(
        reverse('core:accept_project_invite', args=[invite.token])
    )
    role_display = dict(ProjectMembership.ROLE_CHOICES).get(role, 'Developer')
    context = {
        "project":      project,
        "inviter":      request.user,
        "accept_url":   accept_url,
        "role":         role_display,
        "invite_email": email,
    }
    email_warning = None
    try:
        html_content = render_to_string("emails/invite_email.html", context)
        text_content = strip_tags(html_content)
        email_msg = EmailMultiAlternatives(
            subject=f"You're invited to join {project.name}",
            body=text_content,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        email_msg.attach_alternative(html_content, "text/html")
        email_msg.send()
    except Exception as _email_exc:
        import traceback
        print(f"[TF-EMAIL-INV] FAILED to {email}: {_email_exc}")
        traceback.print_exc()
        email_warning = "Invite saved but email could not be delivered."

    response = {
        'status': 'invited',
        'message': 'Invite sent successfully',
        'user': {'email': email, 'role': role},
        'team': team.name if team else None,
        'accept_url': accept_url,
    }
    if email_warning:
        response['warning'] = email_warning
    return JsonResponse(response)


def accept_project_invite(request, token):
    invite = get_object_or_404(ProjectInvite, token=token)

    # Require login — preserve accept URL so allauth returns here after Google OAuth
    if not request.user.is_authenticated:
        accept_path = reverse('core:accept_project_invite', args=[token])
        return redirect(f"{reverse('core:login')}?next={accept_path}")

    # Already used — happens when Google OAuth adapter auto-applied the invite
    if invite.used:
        if invite.project.members.filter(id=request.user.id).exists():
            messages.success(
                request,
                f"You're already a member of '{invite.project.name}' as {invite.role}. Welcome!"
            )
            return redirect('core:project_board', project_id=invite.project.id)
        messages.error(request, "This invitation link has already been used.")
        return redirect('core:workspace_home')

    # Email match check — redirect back to login with the accept URL preserved
    if request.user.email.lower() != invite.email.lower():
        accept_path = reverse('core:accept_project_invite', args=[token])
        messages.error(
            request,
            f"This invite was sent to {invite.email}. "
            "Please sign in with that email address (or via Google if that is your Google account)."
        )
        return redirect(f"{reverse('core:login')}?next={accept_path}")

    # ✅ Add to project members + create/update role
    invite.project.members.add(request.user)
    ProjectMembership.objects.update_or_create(
        user=request.user,
        project=invite.project,
        defaults={'role': invite.role}
    )

    # ✅ Add to team if exists
    if invite.team:
        invite.team.members.add(request.user)

    # ✅ Role assignment (safe)
    Profile.objects.get_or_create(user=request.user)

    # ✅ Mark used
    invite.used = True
    invite.save()

    try:
        # Determine who sent the invite.
        # Priority: the person who created the invite → project lead → project creator.
        # Never fall back to request.user (the new member) to avoid "added by yourself" messages.
        added_by = (
            invite.invited_by
            or invite.project.project_lead
            or invite.project.created_by
        )
        NotificationService().notify_project_member_added(
            invite.project,
            request.user,
            invite.role,
            added_by,
            invite.team,
        )
        if invite.team:
            NotificationService().notify_team_changed(invite.team, request.user, action="joined")
    except Exception:
        pass

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

        if project.members.filter(id=user.id).exists():
            return JsonResponse({'error': 'User is already a member'}, status=400)

        role = request.POST.get('role', 'developer').strip()
        valid_roles = {c[0] for c in ProjectMembership.ROLE_CHOICES if c[0] != 'project_lead'}
        if role not in valid_roles:
            role = 'developer'

        project.members.add(user)
        ProjectMembership.objects.update_or_create(
            user=user,
            project=project,
            defaults={'role': role}
        )
        try:
            NotificationService().notify_project_member_added(project, user, role, request.user)
        except Exception:
            pass

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

    progress_percent = 0
    if total_tasks > 0:
        progress_percent = int((done_tasks / total_tasks) * 100)

    return JsonResponse({
        'status': 'ok',
        'total_tasks': total_tasks,
        'done_tasks': done_tasks,
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

    if _is_project_lead_for_project(user, project) or role == 'admin':
        # 🔹 Full access for project leads and admins
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
        # 'done' is the only terminal status — in_review/qa are still in flight.
        completed_count = status_counts.get('done', 0)
        progress_percent = int((completed_count / task_count) * 100)

    # =========================
    # ✅ PERMISSIONS
    # =========================
    can_manage = _can_manage_project_members(user, project)
    is_project_lead = _is_project_lead_for_project(user, project) or role == 'admin'

    # =========================
    # ✅ PROJECT-SPECIFIC ROLE
    # =========================
    # Used by the template badge and action buttons.
    # Admin keeps their 'admin' label; project leads show 'project_lead';
    # everyone else shows their project membership role.
    membership = ProjectMembership.objects.filter(user=user, project=project).first()
    if role == 'admin':
        template_role = 'admin'
    elif (
        user == project.project_lead
        or (membership and membership.role == 'project_lead')
        or role == 'project_lead'
    ):
        template_role = 'project_lead'
    elif membership:
        template_role = membership.role
    else:
        template_role = role

    # =========================
    # ✅ MEMBER FILTER FOR CREATE-TEAM MODAL
    # =========================
    pm_lead_ids = ProjectMembership.objects.filter(
        project=project,
        role='project_lead'
    ).values_list('user_id', flat=True)

    valid_members = project.members.exclude(id__in=pm_lead_ids).distinct()

    # Repair stale PM rows where role='admin' but user is a project lead
    ProjectMembership.objects.filter(
        project=project,
        role='admin',
        user__profile__role='project_lead',
    ).update(role='project_lead')

    # Build a {user_id: role_display} map so member cards show the
    # project-specific membership role, not the global profile role.
    _role_choices = dict(ProjectMembership.ROLE_CHOICES)
    member_project_role = {}
    for pm in ProjectMembership.objects.filter(
        project=project, user__in=valid_members
    ).select_related('user__profile'):
        if pm.role == 'admin' and pm.user.profile.role == 'project_lead':
            member_project_role[pm.user_id] = 'Project Lead'
        else:
            member_project_role[pm.user_id] = _role_choices.get(
                pm.role, pm.role.replace('_', ' ').title()
            )

    lead_candidates = valid_members

    # Pair each member with their project-specific role display so the
    # template can show "Developer" instead of "Admin" for a member whose
    # global profile.role is admin but whose membership role is developer.
    valid_members_with_roles = [
        (m, member_project_role.get(m.id, m.profile.get_role_display()))
        for m in valid_members
    ]

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
        'role': template_role,
        'progress_percent': progress_percent,
        'valid_members': valid_members,
        'valid_members_with_roles': valid_members_with_roles,
        'lead_candidates': lead_candidates,
        'project_timeline': _project_timeline_context(project),
    })


@login_required
def toggle_user(request, id):
    if not _is_admin_user(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    if request.method != "POST":
        return JsonResponse({"error": "Invalid request"}, status=400)

    user = get_object_or_404(User, id=id)

    if user == request.user:
        return JsonResponse({"error": "You cannot deactivate yourself"}, status=400)

    if user.is_superuser:
        return JsonResponse({"error": "Cannot modify superuser"}, status=400)

    user.is_active = not user.is_active
    user.save(update_fields=["is_active"])

    return JsonResponse({"status": "ok", "is_active": user.is_active})


@login_required
def user_stats(request):
    if not _is_admin_user(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    return JsonResponse({
        "total": User.objects.count(),
        "admins": User.objects.filter(is_superuser=True).count(),
        "active": User.objects.filter(is_active=True).count()
    })

#  USER LIST

@login_required
@role_required(['admin'])
def user_list(request):

    if not _is_admin_user(request.user):
        return redirect('core:home')

    query = request.GET.get('q', '')
    provider = request.GET.get('provider')  # google / manual

    users = User.objects.select_related('profile').annotate(
        is_google=Case(
            When(profile__oauth_provider__iexact='google', then=Value(1)),
            default=Value(0),
            output_field=IntegerField(),
        )
    )

    if query:
        users = users.filter(username__icontains=query)

    if provider == "google":
        users = users.filter(profile__oauth_provider__iexact='google')
    elif provider == "manual":
        users = users.exclude(profile__oauth_provider__iexact='google')

    users = users.order_by('-is_google', 'username')

    paginator = Paginator(users, 5)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'core/user_list.html', {
        'page_obj': page_obj,
        'query': query,
        'total_users': users.count(),
        'total_admins': User.objects.filter(is_superuser=True).count(),
        'active_users': User.objects.filter(is_active=True).count(),
    })

@login_required
@role_required(['admin'])
def promote_to_project_lead(request, id):

    if not _is_admin_user(request.user):
        return redirect('core:home')

    user = get_object_or_404(User, id=id)
    profile, _ = Profile.objects.get_or_create(user=user)

    if profile.isProjectLead:
        profile.isProjectLead = False
        profile.role = 'developer'
        messages.success(request, f"❌ {user.username} removed from Project Lead")
    else:
        profile.isProjectLead = True
        profile.role = 'project_lead'

        if not profile.department:
            messages.warning(request, "⚠️ Assign department to this user")

        messages.success(request, f"✅ {user.username} promoted to Project Lead")

    profile.save(update_fields=["isProjectLead", "role"])

    return redirect('core:user_list')


@login_required
def api_users(request):
    if not _is_admin_user(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    provider = request.GET.get("provider")

    qs = Profile.objects.select_related("user")

    if provider == "google":
        qs = qs.filter(oauth_provider__iexact="google")
    elif provider == "manual":
        qs = qs.exclude(oauth_provider__iexact="google")

    data = [
        {
            "id": p.user_id,
            "username": p.user.username,
            "email": p.user.email,
            "role": p.role,
            "isProjectLead": p.isProjectLead,
            "department": p.department,
            "is_active": p.user.is_active,
        }
        for p in qs.order_by("-user__id")
    ]

    return JsonResponse({"users": data})


@login_required
def api_toggle_project_lead(request, id):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    if not _is_admin_user(request.user):
        return JsonResponse({"error": "Unauthorized"}, status=403)

    user = get_object_or_404(User, id=id)
    profile, _ = Profile.objects.get_or_create(user=user)

    profile.isProjectLead = not profile.isProjectLead
    profile.role = 'project_lead' if profile.isProjectLead else 'developer'

    profile.save(update_fields=["isProjectLead", "role"])

    return JsonResponse({
        "status": "ok",
        "id": user.id,
        "isProjectLead": profile.isProjectLead
    })

#  CREATE USER

from django.contrib import messages
@login_required
@role_required(['admin'])
def user_create(request):

    if not _is_admin_user(request.user):
        return redirect('core:home')

    if request.method == "POST":
        form = UserCreateForm(request.POST)

        if form.is_valid():
            try:
                user = form.save()  # ✅ FULL CONTROL TO FORM

                # Manual (non-Google) accounts need to prove they own the
                # inbox before they can sign in — Google-linked rows get an
                # unusable password (see UserCreateForm.save) so they can
                # only ever get in via real OAuth, which proves it for free.
                if user.profile.oauth_provider != 'google':
                    user.profile.email_verified = False
                    user.profile.save(update_fields=["email_verified"])
                    _send_verification_email(request, user)

                messages.success(request, f"User '{user.username}' created successfully 🚀")
                return redirect('core:user_list')

            except Exception as e:
                messages.error(request, f"Error: {str(e)}")
        else:
            print(form.errors)  # 🔥 DEBUG (check console)
            messages.error(request, "Form validation failed")

    else:
        form = UserCreateForm()

    return render(request, 'core/user_create.html', {'form': form})


@login_required
@role_required(['admin'])
def user_update(request, id):

    if not _is_admin_user(request.user):
        return redirect('core:home')

    user = get_object_or_404(User, id=id)
    # Captured before form validation — ModelForm's _post_clean() writes
    # cleaned values onto this same `user` instance as soon as is_valid()
    # runs, so grabbing this later (even "before save()") would be too late.
    original_email = user.email

    if request.method == "POST":
        form = UserUpdateForm(request.POST, instance=user)

        if form.is_valid():
            user = form.save()

            profile, _ = Profile.objects.get_or_create(user=user)

            email_changed = original_email.strip().lower() != user.email.strip().lower()
            if email_changed and profile.oauth_provider != 'google':
                profile.email_verified = False
                profile.save(update_fields=["email_verified"])
                _send_verification_email(request, user)

            # Once a user is Project Lead the role is locked — ignore any
            # attempt to downgrade it through this form.
            if profile.role == "project_lead":
                new_role = "project_lead"
            else:
                new_role = form.cleaned_data.get("role") or "user"

            profile.role = new_role
            profile.isProjectLead = (new_role == "project_lead")
            profile.save(update_fields=["role", "isProjectLead"])

            # Map system role → project membership role
            _pm_role_map = {
                "project_lead":    "project_lead",
                "developer":       "developer",
                "tester":          "tester",
                "ui_ux_designer":  "ui_ux_designer",
                "delivery_team":   "delivery_team",
                "deployment_team": "deployment_team",
                "admin":           "developer",
                "user":            "developer",
            }
            membership_role = _pm_role_map.get(new_role, "developer")

            assigned_projects = form.cleaned_data.get("assigned_projects", [])

            ProjectMembership.objects.filter(user=user).exclude(
                project__in=assigned_projects
            ).delete()

            for project in assigned_projects:
                ProjectMembership.objects.update_or_create(
                    user=user,
                    project=project,
                    defaults={"role": membership_role}
                )

            messages.success(request, f"{user.username} updated successfully")
            return redirect("core:user_list")

    else:
        form = UserUpdateForm(instance=user)

    return render(request, "core/user_update.html", {
        "form": form,
        "user_obj": user,
    })

@login_required
@role_required(['admin'])
def user_delete(request, id):

    if not _is_admin_user(request.user):
        return redirect('core:home')

    user = get_object_or_404(User, id=id)

    if request.user == user:
        messages.error(request, "You cannot delete your own account.")
        return redirect("core:user_list")

    if request.method == "POST":
        username = user.username
        user.delete()
        messages.success(request, f"User '{username}' deleted successfully.")
        return redirect("core:user_list")

    return render(request, "core/user_delete.html", {"user": user})


@login_required
def test_email_send(request):
    """Admin-only endpoint to diagnose and verify email configuration.

    GET  → shows full config + SMTP connection test (no email sent)
    POST → sends a real test email to the logged-in admin's address
    """
    import smtplib
    import ssl as _ssl

    if not _is_admin_user(request.user):
        return JsonResponse({'error': 'Unauthorized'}, status=403)

    backend  = getattr(settings, 'EMAIL_BACKEND', '')
    host     = getattr(settings, 'EMAIL_HOST', '')
    port     = getattr(settings, 'EMAIL_PORT', 587)
    use_tls  = getattr(settings, 'EMAIL_USE_TLS', False)
    timeout  = getattr(settings, 'EMAIL_TIMEOUT', 30)
    user_var = getattr(settings, 'EMAIL_HOST_USER', '') or ''
    pwd_var  = getattr(settings, 'EMAIL_HOST_PASSWORD', '') or ''

    # Show masked password diagnostics — helps confirm quote-stripping worked
    if pwd_var:
        pwd_diag = f"{pwd_var[0]}{'*' * (len(pwd_var) - 2)}{pwd_var[-1]}  (len={len(pwd_var)}, first_char={repr(pwd_var[0])})"
    else:
        pwd_diag = '(empty — SMTP auth will fail)'

    config_summary = {
        'EMAIL_BACKEND':      backend,
        'EMAIL_HOST':         host,
        'EMAIL_PORT':         port,
        'EMAIL_USE_TLS':      use_tls,
        'EMAIL_HOST_USER':    user_var or '(not set)',
        'EMAIL_HOST_PASSWORD': pwd_diag,
        'EMAIL_TIMEOUT':      timeout,
        'DEFAULT_FROM_EMAIL': getattr(settings, 'DEFAULT_FROM_EMAIL', ''),
        'smtp_backend_active': 'smtp' in backend,
    }

    # ── Connection probe (GET only — safe, no email sent) ───────────────────
    smtp_probe = None
    if request.method == 'GET':
        if 'ResendAPIBackend' in backend:
            # Probe Resend HTTP API — check API key is valid
            import requests as _req
            try:
                r = _req.get(
                    'https://api.resend.com/domains',
                    headers={'Authorization': f'Bearer {pwd_var}'},
                    timeout=10,
                )
                if r.status_code == 200:
                    smtp_probe = 'OK — Resend API key valid (HTTP 200)'
                elif r.status_code == 401:
                    smtp_probe = f'AUTH FAILED — invalid Resend API key (HTTP 401)'
                else:
                    smtp_probe = f'Resend API responded HTTP {r.status_code}: {r.text[:200]}'
            except Exception as e:
                smtp_probe = f'CONNECTION ERROR — {e}'
        elif 'smtp' in backend and host and user_var and pwd_var:
            try:
                import ssl as _ssl
                ctx = _ssl.create_default_context()
                with smtplib.SMTP(host, port, timeout=10) as conn:
                    if use_tls:
                        conn.starttls(context=ctx)
                    conn.login(user_var, pwd_var)
                    smtp_probe = 'OK — SMTP login succeeded'
            except smtplib.SMTPAuthenticationError as e:
                smtp_probe = f'AUTH FAILED — wrong password or App Password not enabled: {e}'
            except Exception as e:
                smtp_probe = f'CONNECTION ERROR — {e}'

    # Allow GET + ?send=1 to trigger test email without needing CSRF POST
    send_now = request.method == 'POST' or request.GET.get('send') == '1'

    if not send_now:
        return JsonResponse({'config': config_summary, 'smtp_probe': smtp_probe})

    # ── Send real test email ────────────────────────────────────────────────
    recipient = request.user.email
    if not recipient:
        return JsonResponse({'error': 'Your admin account has no email address set.'}, status=400)

    try:
        email_msg = EmailMultiAlternatives(
            subject='VetriFlow — Email Config Test',
            body=f'Test email from VetriFlow.\nBackend: {backend}\nHost: {host}:{port}',
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[recipient],
        )
        email_msg.attach_alternative(
            f'<p><strong>VetriFlow email config is working!</strong></p>'
            f'<p>Backend: <code>{backend}</code><br>'
            f'Host: <code>{host}:{port}</code><br>'
            f'Sent to: <code>{recipient}</code></p>',
            'text/html',
        )
        email_msg.send()
        return JsonResponse({'status': 'sent', 'to': recipient, 'config': config_summary})
    except Exception as e:
        return JsonResponse({'status': 'failed', 'error': str(e), 'config': config_summary}, status=500)
    
    
@login_required
def reports_view(request):
    profile = request.user.profile
    role = profile.role

    # Filters
    project_id = request.GET.get('project')
    status = request.GET.get('status')
    user_id = request.GET.get('user')

    # Base Query (role-based)
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

    from datetime import date as _date
    return render(request, 'core/reports.html', {
        'tasks': tasks[:50],
        'projects': projects,
        'users': users,
        'status_summary': status_summary,
        'project_summary': project_summary,
        'selected_project': project_id,
        'selected_status': status,
        'selected_user': user_id,
        'today': _date.today(),
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

@login_required(login_url='core:login')
def notification_history(request):
    """Full notification history page (HTML) — the topbar dropdown's 'View all
    notifications' link used to point straight at get_notifications, which is
    a JSON API, so clicking it showed raw JSON instead of a page."""
    notifications_qs = Notification.objects.filter(user=request.user).order_by('-created_at')
    paginator = Paginator(notifications_qs, 20)
    page_obj = paginator.get_page(request.GET.get('page'))

    return render(request, 'core/notification_history.html', {
        'page_obj': page_obj,
        'notification_count': _get_notification_count(request.user),
    })


@login_required
def get_notifications(request):
    """AJAX endpoint to get pending notifications for the current user"""
    if request.method != 'GET':
        return JsonResponse({'status': 'error', 'message': 'Invalid request'}, status=400)
    
    base_qs = Notification.objects.filter(user=request.user, is_read=False)
    total_unread = base_qs.count()  # true total BEFORE slicing
    notifications = base_qs.order_by('-created_at')[:10]

    data = {
        'status': 'ok',
        'count': total_unread,
        'notifications': [
            {
                'id': n.id,
                'notification_type': n.notification_type,
                'is_read': n.is_read,
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


# =============================================================================
#  PIPELINE SETTINGS  (Admin only)
# =============================================================================

# All pipeline stages in their canonical order
_PIPELINE_STAGES = DepartmentPipelineSettings.STAGE_CHOICES   # list of (key, label)
_DEPARTMENT_CHOICES = DepartmentPipelineSettings.DEPARTMENT_CHOICES


def _get_pipeline_settings_map(department: str) -> dict:
    """Return {stage_key: is_visible} for a department, defaulting True for missing rows."""
    saved = DepartmentPipelineSettings.objects.filter(department=department)
    mapping = {row.stage_key: row.is_visible for row in saved}
    for key, _ in _PIPELINE_STAGES:
        mapping.setdefault(key, True)
    return mapping


@login_required(login_url='core:login')
def pipeline_settings(request):
    """Admin HTML page: view and configure per-department pipeline visibility."""
    if not _is_admin_user(request.user):
        return HttpResponseForbidden('Admin access required.')

    selected_dept = request.GET.get('department', _DEPARTMENT_CHOICES[0][0])
    settings_map = _get_pipeline_settings_map(selected_dept)

    stages_with_visibility = [
        {'key': key, 'label': label, 'is_visible': settings_map.get(key, True)}
        for key, label in _PIPELINE_STAGES
    ]

    return render(request, 'core/pipeline_settings.html', {
        'departments': _DEPARTMENT_CHOICES,
        'selected_dept': selected_dept,
        'selected_dept_label': dict(_DEPARTMENT_CHOICES).get(selected_dept, selected_dept),
        'stages': stages_with_visibility,
    })


@login_required(login_url='core:login')
def api_pipeline_settings(request):
    """
    GET  → return visibility map for ?department=<key>
    PATCH → update visibility for a department (admin only)
    """
    if request.method == 'GET':
        dept = request.GET.get('department')
        if not dept or dept not in dict(_DEPARTMENT_CHOICES):
            return JsonResponse({'error': 'Invalid department'}, status=400)
        mapping = _get_pipeline_settings_map(dept)
        return JsonResponse({
            'department': dept,
            'stages': [
                {'key': k, 'label': l, 'is_visible': mapping[k]}
                for k, l in _PIPELINE_STAGES
            ]
        })

    if request.method == 'PATCH':
        if not _is_admin_user(request.user):
            return JsonResponse({'error': 'Admin access required.'}, status=403)
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        dept = body.get('department')
        visibility = body.get('stages')  # {stage_key: bool}

        if not dept or dept not in dict(_DEPARTMENT_CHOICES):
            return JsonResponse({'error': 'Invalid department'}, status=400)
        if not isinstance(visibility, dict):
            return JsonResponse({'error': 'stages must be an object'}, status=400)

        valid_keys = {k for k, _ in _PIPELINE_STAGES}
        with transaction.atomic():
            for stage_key, is_visible in visibility.items():
                if stage_key not in valid_keys:
                    continue
                DepartmentPipelineSettings.objects.update_or_create(
                    department=dept,
                    stage_key=stage_key,
                    defaults={'is_visible': bool(is_visible), 'updated_by': request.user},
                )

        return JsonResponse({'status': 'ok', 'department': dept})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required(login_url='core:login')
def api_department_stages(request, department):
    """Public (auth-required) endpoint: GET visible stage keys for a department."""
    if department not in dict(_DEPARTMENT_CHOICES):
        return JsonResponse({'error': 'Invalid department'}, status=400)
    mapping = _get_pipeline_settings_map(department)
    visible = [k for k, _ in _PIPELINE_STAGES if mapping.get(k, True)]
    return JsonResponse({'department': department, 'visible_stages': visible})


# =============================================================================
#  TEAMS & DEPARTMENTS ADMIN  (admin only)
# =============================================================================

def _admin_required(view_fn):
    """Decorator: 403 for non-admin users."""
    from functools import wraps
    @wraps(view_fn)
    @login_required(login_url='core:login')
    def wrapped(request, *args, **kwargs):
        if not _is_admin_user(request.user):
            return JsonResponse({'error': 'Admin access required.'}, status=403)
        return view_fn(request, *args, **kwargs)
    return wrapped


@login_required(login_url='core:login')
def teams_departments(request):
    """Admin management page for IT Departments, IT Teams, and Stage Sequences."""
    if not _is_admin_user(request.user):
        return HttpResponseForbidden('Admin access required.')

    departments = ITDepartment.objects.prefetch_related('it_teams').order_by('name')
    global_stages = list(
        ProjectStageConfiguration.objects
        .filter(project=None)
        .select_related('team__department')
        .order_by('seq_order')
    )
    all_teams = list(ITTeam.objects.select_related('department').order_by('department__name', 'name'))
    projects  = list(Project.objects.order_by('name').values('id', 'name'))

    return render(request, 'core/teams_departments.html', {
        'departments':    departments,
        'global_stages':  global_stages,
        'all_teams':      all_teams,
        'team_types':     ITTeam.TEAM_TYPE_CHOICES,
        'projects':       projects,
    })


# ── IT Department CRUD ────────────────────────────────────────────────────────

@_admin_required
def api_it_department_create(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    name = (body.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': 'Name is required.'}, status=400)
    if ITDepartment.objects.filter(name__iexact=name).exists():
        return JsonResponse({'error': 'A department with this name already exists.'}, status=400)

    dept = ITDepartment.objects.create(
        name=name,
        description=(body.get('description') or '').strip(),
    )
    return JsonResponse({
        'id': dept.pk, 'name': dept.name, 'description': dept.description,
        'created_at': dept.created_at.strftime('%b %d, %Y'),
    }, status=201)


@_admin_required
def api_it_department_update(request, dept_id):
    if request.method not in ('POST', 'PATCH'):
        return JsonResponse({'error': 'POST/PATCH required'}, status=405)
    dept = get_object_or_404(ITDepartment, pk=dept_id)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    name = (body.get('name') or '').strip()
    if not name:
        return JsonResponse({'error': 'Name is required.'}, status=400)
    if ITDepartment.objects.filter(name__iexact=name).exclude(pk=dept_id).exists():
        return JsonResponse({'error': 'A department with this name already exists.'}, status=400)

    dept.name        = name
    dept.description = (body.get('description') or '').strip()
    dept.save()
    return JsonResponse({'id': dept.pk, 'name': dept.name, 'description': dept.description})


@_admin_required
def api_it_department_delete(request, dept_id):
    if request.method not in ('POST', 'DELETE'):
        return JsonResponse({'error': 'POST/DELETE required'}, status=405)
    dept = get_object_or_404(ITDepartment, pk=dept_id)
    dept.delete()
    return JsonResponse({'status': 'deleted', 'id': dept_id})


# ── IT Team CRUD ──────────────────────────────────────────────────────────────

@_admin_required
def api_it_team_create(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    name      = (body.get('name') or '').strip()
    dept_id   = body.get('department_id')
    team_type = body.get('team_type', 'development')
    color     = (body.get('color') or '#4F46E5').strip()

    if not name:
        return JsonResponse({'error': 'Name is required.'}, status=400)
    if not dept_id:
        return JsonResponse({'error': 'Department is required.'}, status=400)
    if ITTeam.objects.filter(name__iexact=name).exists():
        return JsonResponse({'error': 'A team with this name already exists.'}, status=400)

    dept = get_object_or_404(ITDepartment, pk=dept_id)
    team = ITTeam.objects.create(
        name=name, department=dept, team_type=team_type, color=color
    )
    return JsonResponse({
        'id': team.pk, 'name': team.name,
        'department_id': dept.pk, 'department_name': dept.name,
        'team_type': team.team_type, 'team_type_label': team.get_team_type_display(),
        'color': team.color,
        'created_at': team.created_at.strftime('%b %d, %Y'),
    }, status=201)


@_admin_required
def api_it_team_update(request, team_id):
    if request.method not in ('POST', 'PATCH'):
        return JsonResponse({'error': 'POST/PATCH required'}, status=405)
    team = get_object_or_404(ITTeam, pk=team_id)
    try:
        body = json.loads(request.body)
    except ValueError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)

    name      = (body.get('name') or '').strip()
    team_type = body.get('team_type', team.team_type)
    color     = (body.get('color') or team.color).strip()
    dept_id   = body.get('department_id', team.department_id)

    if not name:
        return JsonResponse({'error': 'Name is required.'}, status=400)
    if ITTeam.objects.filter(name__iexact=name).exclude(pk=team_id).exists():
        return JsonResponse({'error': 'A team with this name already exists.'}, status=400)

    dept = get_object_or_404(ITDepartment, pk=dept_id)
    team.name       = name
    team.department = dept
    team.team_type  = team_type
    team.color      = color
    team.save()
    return JsonResponse({
        'id': team.pk, 'name': team.name,
        'department_id': dept.pk, 'department_name': dept.name,
        'team_type': team.team_type, 'team_type_label': team.get_team_type_display(),
        'color': team.color,
    })


@_admin_required
def api_it_team_delete(request, team_id):
    if request.method not in ('POST', 'DELETE'):
        return JsonResponse({'error': 'POST/DELETE required'}, status=405)
    team = get_object_or_404(ITTeam, pk=team_id)
    team.delete()
    return JsonResponse({'status': 'deleted', 'id': team_id})


# ── Stage Sequence CRUD ───────────────────────────────────────────────────────

@login_required(login_url='core:login')
def api_stage_sequence(request):
    """
    GET  ?project_id=<id>|global  → return current sequence
    POST {project_id|null, stages:[{team_id, seq_order, label_override}]}
         → upsert sequence (admin only for POST)
    """
    if request.method == 'GET':
        project_id = request.GET.get('project_id')
        if project_id and project_id != 'global':
            try:
                project_id = int(project_id)
            except ValueError:
                return JsonResponse({'error': 'Invalid project_id'}, status=400)
            qs = ProjectStageConfiguration.objects.filter(project_id=project_id)
        else:
            qs = ProjectStageConfiguration.objects.filter(project=None)

        stages = list(
            qs.select_related('team__department').order_by('seq_order').values(
                'id', 'seq_order', 'label_override',
                'team_id', 'team__name', 'team__color', 'team__team_type',
                'team__department__name',
            )
        )
        return JsonResponse({'stages': stages})

    if request.method == 'POST':
        if not _is_admin_user(request.user):
            return JsonResponse({'error': 'Admin access required.'}, status=403)
        try:
            body = json.loads(request.body)
        except ValueError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)

        raw_project = body.get('project_id')
        if raw_project and raw_project != 'global':
            project_obj = get_object_or_404(Project, pk=raw_project)
        else:
            project_obj = None

        stages_data = body.get('stages', [])
        if not isinstance(stages_data, list):
            return JsonResponse({'error': 'stages must be an array'}, status=400)

        with transaction.atomic():
            # Delete existing config for this scope
            ProjectStageConfiguration.objects.filter(project=project_obj).delete()
            # Re-create in submitted order
            for item in stages_data:
                team_id  = item.get('team_id')
                order    = item.get('seq_order', 0)
                label    = (item.get('label_override') or '').strip()
                if not team_id:
                    continue
                team = get_object_or_404(ITTeam, pk=team_id)
                ProjectStageConfiguration.objects.create(
                    project=project_obj,
                    team=team,
                    seq_order=order,
                    label_override=label,
                )

        return JsonResponse({'status': 'ok', 'project_id': project_obj.pk if project_obj else None})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


