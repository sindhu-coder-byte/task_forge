from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView, TokenVerifyView

from core.api.views_admin import (
    ITDepartmentDetailView,
    ITDepartmentListCreateView,
    ITTeamDetailView,
    ITTeamListCreateView,
    PipelineSettingsView,
    StageSequenceView,
    ToggleProjectLeadView,
    UserListView,
)
from core.api.views_auth import LoginView, MeView, TokenExchangeView
from core.api.views_notifications import (
    MarkAllNotificationsReadView,
    MarkNotificationReadView,
    NotificationHistoryView,
    NotificationListView,
)
from core.api.views_projects import ProjectViewSet
from core.api.views_reports import ReportsView
from core.api.views_search import SearchView
from core.api.views_tasks import TaskViewSet
from core.api.views_teams import TeamViewSet

app_name = 'core_api'

router = DefaultRouter()
router.register('projects', ProjectViewSet, basename='project')
router.register('tasks', TaskViewSet, basename='task')
router.register('teams', TeamViewSet, basename='team')

urlpatterns = [
    path('auth/login/', LoginView.as_view(), name='login'),
    path('auth/token-exchange/', TokenExchangeView.as_view(), name='token_exchange'),
    path('auth/token-refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('auth/token-verify/', TokenVerifyView.as_view(), name='token_verify'),
    path('auth/me/', MeView.as_view(), name='me'),
    path('search/', SearchView.as_view(), name='search'),
    path('notifications/', NotificationListView.as_view(), name='notifications'),
    path('notifications/history/', NotificationHistoryView.as_view(), name='notification_history'),
    path('notifications/<int:notification_id>/read/', MarkNotificationReadView.as_view(), name='mark_notification_read'),
    path('notifications/mark-all-read/', MarkAllNotificationsReadView.as_view(), name='mark_all_notifications_read'),

    # Admin
    path('admin/users/', UserListView.as_view(), name='admin_users'),
    path('admin/users/<int:user_id>/toggle-project-lead/', ToggleProjectLeadView.as_view(), name='admin_toggle_project_lead'),
    path('admin/pipeline-settings/', PipelineSettingsView.as_view(), name='admin_pipeline_settings'),
    path('admin/it-departments/', ITDepartmentListCreateView.as_view(), name='admin_it_departments'),
    path('admin/it-departments/<int:dept_id>/', ITDepartmentDetailView.as_view(), name='admin_it_department_detail'),
    path('admin/it-teams/', ITTeamListCreateView.as_view(), name='admin_it_teams'),
    path('admin/it-teams/<int:team_id>/', ITTeamDetailView.as_view(), name='admin_it_team_detail'),
    path('admin/stage-sequence/', StageSequenceView.as_view(), name='admin_stage_sequence'),

    path('reports/', ReportsView.as_view(), name='reports'),

    path('', include(router.urls)),
]
