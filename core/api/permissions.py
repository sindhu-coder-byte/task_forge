"""DRF permission wrappers around the role-check helpers already used by the
legacy web views (core/views.py). Kept as thin wrappers rather than a parallel
implementation so web and mobile authorization can't drift apart.
"""
from rest_framework.permissions import BasePermission

from core.views import (
    _can_manage_project_members,
    _is_admin_user,
    _is_project_lead_for_project,
    _project_accessible_by,
    _task_can_view,
)


class IsAdminUser(BasePermission):
    def has_permission(self, request, view):
        return _is_admin_user(request.user)


class IsProjectAccessible(BasePermission):
    """Object-level permission for a Project instance."""

    def has_object_permission(self, request, view, obj):
        return _project_accessible_by(request.user, obj)


class IsProjectLeadForProject(BasePermission):
    def has_object_permission(self, request, view, obj):
        return _is_admin_user(request.user) or _is_project_lead_for_project(request.user, obj)


class CanManageProjectMembers(BasePermission):
    def has_object_permission(self, request, view, obj):
        return _can_manage_project_members(request.user, obj)


class CanViewTask(BasePermission):
    def has_object_permission(self, request, view, obj):
        return _task_can_view(request.user, obj)
