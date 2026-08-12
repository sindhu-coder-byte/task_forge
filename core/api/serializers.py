from django.contrib.auth.models import User
from rest_framework import serializers

from core.models import Comment, Label, Profile, Project, ProjectMembership, Task, Team


class ProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = Profile
        fields = ['role', 'isProjectLead', 'oauth_provider', 'email_verified']


class UserSerializer(serializers.ModelSerializer):
    profile = ProfileSerializer(read_only=True)

    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'is_active', 'profile']


class UserSummarySerializer(serializers.ModelSerializer):
    """Lightweight user reference for nesting inside project/task payloads."""

    class Meta:
        model = User
        fields = ['id', 'username', 'email']


class LabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = ['id', 'name', 'color']


class ProjectMembershipSerializer(serializers.ModelSerializer):
    user = UserSummarySerializer(read_only=True)
    role_display = serializers.CharField(source='get_role_display', read_only=True)

    class Meta:
        model = ProjectMembership
        fields = ['id', 'user', 'role', 'role_display']


class ProjectSerializer(serializers.ModelSerializer):
    project_lead = UserSummarySerializer(read_only=True)
    created_by = UserSummarySerializer(read_only=True)
    total_tasks = serializers.IntegerField(read_only=True)
    done_tasks = serializers.IntegerField(read_only=True)
    progress_pct = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'description', 'created_by', 'project_lead', 'department',
            'category', 'project_url', 'start_date', 'target_end_date', 'priority',
            'is_private', 'project_type', 'key_prefix', 'created_at',
            'total_tasks', 'done_tasks', 'progress_pct',
        ]

    def get_progress_pct(self, obj):
        total = getattr(obj, 'total_tasks', 0) or 0
        done = getattr(obj, 'done_tasks', 0) or 0
        return int(done / total * 100) if total else 0


class ProjectDetailSerializer(ProjectSerializer):
    members = UserSummarySerializer(many=True, read_only=True)

    class Meta(ProjectSerializer.Meta):
        fields = ProjectSerializer.Meta.fields + ['members']


class TaskListSerializer(serializers.ModelSerializer):
    assigned_to = UserSummarySerializer(read_only=True)
    reporter = UserSummarySerializer(read_only=True)
    issue_key = serializers.CharField(read_only=True)

    class Meta:
        model = Task
        fields = [
            'id', 'issue_key', 'title', 'issue_type', 'status', 'priority',
            'project', 'assigned_to', 'reporter', 'due_date', 'created_at',
        ]


class TaskDetailSerializer(TaskListSerializer):
    labels = LabelSerializer(many=True, read_only=True)
    created_by = UserSummarySerializer(read_only=True)

    class Meta(TaskListSerializer.Meta):
        fields = TaskListSerializer.Meta.fields + [
            'description', 'parent', 'team', 'labels', 'created_by',
            'start_date', 'delivery_date',
        ]


class TeamSerializer(serializers.ModelSerializer):
    lead = UserSummarySerializer(read_only=True)
    members = UserSummarySerializer(many=True, read_only=True)
    total_members = serializers.IntegerField(read_only=True)
    workload = serializers.IntegerField(read_only=True)
    completed_tasks = serializers.IntegerField(read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = Team
        fields = [
            'id', 'name', 'slug', 'description', 'project', 'project_name', 'lead', 'members',
            'team_type', 'color', 'capacity', 'is_active',
            'total_members', 'workload', 'completed_tasks',
        ]


class CommentSerializer(serializers.ModelSerializer):
    user = UserSummarySerializer(read_only=True)

    class Meta:
        model = Comment
        fields = ['id', 'task', 'user', 'text', 'created']
        read_only_fields = ['task', 'user']
