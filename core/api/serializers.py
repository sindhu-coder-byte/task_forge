from django.contrib.auth.models import User
from django.db import transaction
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


class ProjectCreateSerializer(serializers.ModelSerializer):
    """Mirrors core.views.create_project's validation (key-prefix generation/
    collision handling, creator-as-project-lead membership) — see
    ProjectViewSet.create in views_projects.py for how this is wired in.
    """

    class Meta:
        model = Project
        fields = [
            'id', 'name', 'description', 'project_type', 'key_prefix', 'category',
            'project_url', 'start_date', 'target_end_date', 'priority', 'is_private',
        ]
        read_only_fields = ['id']
        extra_kwargs = {
            'key_prefix': {'required': False, 'allow_blank': True},
            'description': {'required': False, 'allow_blank': True},
        }

    def validate(self, attrs):
        start = attrs.get('start_date')
        end = attrs.get('target_end_date')
        if start and end and end < start:
            raise serializers.ValidationError(
                {'target_end_date': 'Due date cannot be before start date.'}
            )
        return attrs

    def create(self, validated_data):
        from core.views import _generate_project_key_prefix

        raw_key_prefix = (validated_data.pop('key_prefix', '') or '').strip()
        key_prefix = (raw_key_prefix or _generate_project_key_prefix(validated_data.get('name', ''))).strip().upper()[:10] or 'TF'

        if Project.objects.filter(key_prefix__iexact=key_prefix).exists():
            if raw_key_prefix:
                raise serializers.ValidationError({
                    'key_prefix': f'Key prefix "{key_prefix}" is already in use by another project. '
                                   'Please choose a different one.',
                })
            # Auto-generated prefix collided — disambiguate quietly, same as the web form.
            base_prefix = key_prefix[:9]
            suffix = 2
            while Project.objects.filter(key_prefix__iexact=f'{base_prefix}{suffix}').exists():
                suffix += 1
            key_prefix = f'{base_prefix}{suffix}'

        request = self.context['request']
        with transaction.atomic():
            project = Project.objects.create(
                created_by=request.user,
                key_prefix=key_prefix,
                **validated_data,
            )
            project.members.add(request.user)
            ProjectMembership.objects.update_or_create(
                user=request.user, project=project, defaults={'role': 'project_lead'},
            )
        return project


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


class TaskCreateSerializer(serializers.ModelSerializer):
    """Mirrors core.views.create_task's validation — the requesting user must
    have access to the project, and the assignee must be a member/lead of it
    (the "JIRA rule", views.py:1925-1928). issue_number is allocated
    automatically by Task.save() (core/models.py:383-392), so it isn't set
    here.
    """

    class Meta:
        model = Task
        fields = ['id', 'title', 'description', 'project', 'issue_type', 'assigned_to', 'priority', 'due_date']
        read_only_fields = ['id']
        extra_kwargs = {'description': {'required': False, 'allow_blank': True}}

    def validate(self, attrs):
        from core.views import _is_admin_user, _project_accessible_by

        request = self.context['request']
        project = attrs.get('project')
        assigned_to = attrs.get('assigned_to')

        if project and not _is_admin_user(request.user) and not _project_accessible_by(request.user, project):
            raise serializers.ValidationError({'project': 'You do not have access to this project.'})

        if project and assigned_to:
            is_member = project.members.filter(id=assigned_to.id).exists()
            is_lead = project.project_lead_id == assigned_to.id
            if not (is_member or is_lead):
                raise serializers.ValidationError({'assigned_to': 'User is not part of this project.'})

        return attrs

    def create(self, validated_data):
        request = self.context['request']
        return Task.objects.create(
            created_by=request.user,
            reporter=request.user,
            **validated_data,
        )


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
