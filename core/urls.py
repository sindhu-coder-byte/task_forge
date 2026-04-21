from django.urls import path
from . import views

app_name = 'core'

urlpatterns = [

    # =========================
    #  HOME & DASHBOARD
    # =========================
    path('', views.home, name='home'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('dashboard/user/', views.user_dashboard, name='user_dashboard'),


    # =========================
    #  AUTH
    # =========================
    path('login/', views.login_view, name='login'),
    # path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('password-reset/', views.password_reset_request, name='password-reset'),


    # =========================
    #  USERS (ADMIN)
    # =========================
    path('users/', views.user_list, name='user_list'),
    path('users/create/', views.user_create, name='user_create'),
    path('users/<int:id>/update/', views.user_update, name='user_update'),
    path('users/<int:id>/delete/', views.user_delete, name='user_delete'),
    path("user/stats/", views.user_stats),

    # =========================
    #  PROJECTS
    # =========================
    path('projects/', views.projects, name='projects'),
    path('projects/create/', views.create_project, name='create_project'),
    path('projects/<int:project_id>/', views.project_detail, name='project_detail'),
    path('projects/<int:project_id>/board/', views.project_board, name='project_board'),
    path('projects/<int:project_id>/backlog/', views.project_backlog, name='project_backlog'),
    path('projects/<int:project_id>/team/', views.project_team, name='project_team'),
    path('projects/<int:project_id>/edit/', views.edit_project, name='edit_project'),
    path('projects/<int:project_id>/delete/', views.delete_project, name='delete_project'),

    # Members
    path('projects/<int:project_id>/members/', views.get_project_members, name='project_members'),
    path('projects/<int:project_id>/invite-member/', views.invite_project_member, name='invite_project_member'),
    path('projects/<int:project_id>/add-member/', views.add_project_member, name='add_project_member'),
    path('invite/accept/<str:token>/', views.accept_project_invite, name='accept_project_invite'),
    path('projects/<int:project_id>/remove-member/<int:user_id>/', views.remove_project_member, name='remove_project_member'),
    path('projects/<int:project_id>/progress/', views.get_project_progress, name='get_project_progress'),


    # =========================
    #  TASKS (MAIN)
    # =========================
    path('tasks/', views.tasks, name='tasks'),
    path('tasks/create/', views.create_task, name='create_task'),
    path('tasks/<int:task_id>/', views.task_detail, name='task_detail'),

    #  Status (Jira style)
    path('tasks/<int:task_id>/status/<str:new_status>/', views.update_task_status, name='update_task_status'),


    # =========================
    # TASK ACTIONS
    # =========================
    path('task/<int:task_id>/start/', views.start_task, name='start_task'),
    path('task/<int:task_id>/submit/', views.submit_task, name='submit_task'),
    path('task/<int:task_id>/approve/', views.approve_task, name='approve_task'),
    path('task/<int:task_id>/reject/', views.reject_task, name='reject_task'),


    # =========================
    #  COMMENTS
    # =========================
    path('task/<int:task_id>/comments/', views.get_comments, name='get_comments'),
    path('task/<int:task_id>/add-comment/', views.add_comment, name='add_comment'),


    # =========================
    #  ATTACHMENTS
    # =========================
    path('task/<int:task_id>/upload/', views.upload_attachment, name='upload_attachment'),
    path('task/<int:task_id>/files/', views.get_files, name='get_files'),
    path('task/<int:task_id>/files/<int:attachment_id>/delete/', views.delete_attachment, name='delete_attachment'),

    # Task fields
    path('task/<int:task_id>/due-date/', views.update_task_due_date, name='update_task_due_date'),
    path('task/<int:task_id>/labels/', views.update_task_labels, name='update_task_labels'),


    # =========================
    #  ACTIVITY
    # =========================
    path('task/<int:task_id>/activity/', views.task_activity, name='task_activity'),


    # =========================
    #  SEARCH
    # =========================
    path('search/', views.search_view, name='search'),

    # =========================
    #  TEAMS
    # =========================
    path('teams/', views.teams, name='teams'),
    
    path('reports/', views.reports_view, name='reports'),
    path("role-redirect/", views.role_redirect, name="role_redirect"),

    # =========================
    #  NOTIFICATIONS
    # =========================
    path('notifications/', views.get_notifications, name='get_notifications'),
    path('notifications/<int:notification_id>/read/', views.mark_notification_read, name='mark_notification_read'),
    path('notifications/mark-all-read/', views.mark_all_notifications_read, name='mark_all_notifications_read'),
    path('project/<int:project_id>/member/<int:user_id>/role/', views.update_member_role, name='update_member_role'),
    path('project/<int:project_id>/create-team/', views.create_team, name='create_team'),
]