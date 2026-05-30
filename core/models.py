from django.db import models
from django.contrib.auth.models import User
from django.db import transaction


# ---------------- PROFILE ----------------
class Profile(models.Model):
    ROLE_CHOICES = (
        # ── Platform-level ──────────────────────────────────────────────────
        ('admin',            'Admin'),
        ('user',             'User'),

        # ── Product & Project Management ────────────────────────────────────
        ('project_lead',     'Project Lead'),
        ('product_manager',  'Product Manager'),
        ('business_analyst', 'Business Analyst'),

        # ── Design ──────────────────────────────────────────────────────────
        ('ux_researcher',    'UX Researcher'),
        ('ui_designer',      'UI Designer'),
        ('ui_ux_designer',   'UI/UX Designer'),

        # ── Engineering ─────────────────────────────────────────────────────
        ('frontend_dev',     'Frontend Developer'),
        ('backend_dev',      'Backend Developer'),
        ('fullstack_dev',    'Full-Stack Engineer'),
        ('mobile_dev',       'Mobile Developer'),
        ('dba',              'Database Administrator'),
        ('developer',        'Developer'),

        # ── QA & Testing ────────────────────────────────────────────────────
        ('qa_manual',        'Manual QA Tester'),
        ('qa_automation',    'QA Automation Engineer'),
        ('security_tester',  'Security / Pen Tester'),
        ('tester',           'Tester'),
        ('qa',               'QA'),

        # ── DevOps & Infrastructure ─────────────────────────────────────────
        ('devops_engineer',  'DevOps Engineer'),
        ('cloud_architect',  'Cloud Architect'),
        ('deployment_team',  'Deployment Team'),

        # ── Data Science & Analytics ─────────────────────────────────────────
        ('data_analyst',     'Data Analyst'),
        ('data_scientist',   'Data Scientist'),

        # ── IT Support & Security ────────────────────────────────────────────
        ('helpdesk',         'Helpdesk Technician'),
        ('ciso',             'CISO / SecOps Engineer'),

        # ── Delivery ────────────────────────────────────────────────────────
        ('delivery_team',    'Delivery Team'),
    )

    user = models.OneToOneField(User, on_delete=models.CASCADE)

    # 🔑 GLOBAL ROLE
    role = models.CharField(max_length=25, choices=ROLE_CHOICES, default='user')

    # ⚠️ KEEP (used in your views already)
    isProjectLead = models.BooleanField(default=False)


    # 🔐 AUTH
    oauth_provider = models.CharField(max_length=50, null=True, blank=True)
    oauth_id = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['oauth_provider', 'oauth_id'],
                name='unique_oauth_per_provider',
                condition=~models.Q(oauth_provider=None)  # ✅ prevents NULL conflicts
            )
        ]

    def __str__(self):
        return self.user.username


# ---------------- PROJECT MEMBERSHIP ----------------
class ProjectMembership(models.Model):
    ROLE_CHOICES = (
        # ── Project Leadership ───────────────────────────────────────────────
        ('project_lead',     'Project Lead'),
        ('product_manager',  'Product Manager'),
        ('business_analyst', 'Business Analyst'),

        # ── Design ──────────────────────────────────────────────────────────
        ('ux_researcher',    'UX Researcher'),
        ('ui_designer',      'UI Designer'),
        ('ui_ux_designer',   'UI/UX Designer'),

        # ── Engineering ─────────────────────────────────────────────────────
        ('frontend_dev',     'Frontend Developer'),
        ('backend_dev',      'Backend Developer'),
        ('fullstack_dev',    'Full-Stack Engineer'),
        ('mobile_dev',       'Mobile Developer'),
        ('dba',              'Database Administrator'),
        ('developer',        'Developer'),

        # ── QA & Testing ────────────────────────────────────────────────────
        ('qa_manual',        'Manual QA Tester'),
        ('qa_automation',    'QA Automation Engineer'),
        ('security_tester',  'Security / Pen Tester'),
        ('tester',           'Tester'),
        ('qa',               'QA'),

        # ── DevOps & Infrastructure ──────────────────────────────────────────
        ('devops_engineer',  'DevOps Engineer'),
        ('cloud_architect',  'Cloud Architect'),
        ('deployment_team',  'Deployment Team'),

        # ── Data Science & Analytics ──────────────────────────────────────────
        ('data_analyst',     'Data Analyst'),
        ('data_scientist',   'Data Scientist'),

        # ── IT Support & Security ─────────────────────────────────────────────
        ('helpdesk',         'Helpdesk Technician'),
        ('ciso',             'CISO / SecOps Engineer'),

        # ── Delivery ─────────────────────────────────────────────────────────
        ('delivery_team',    'Delivery Team'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    project = models.ForeignKey('Project', on_delete=models.CASCADE)
    role = models.CharField(max_length=25, choices=ROLE_CHOICES)

    class Meta:
        unique_together = ('user', 'project')
        indexes = [
            models.Index(fields=['project', 'role']),  # ✅ faster filtering
        ]

    def __str__(self):
        return f"{self.user.username} - {self.project.name} - {self.role}"


# ---------------- PROJECT ----------------
class Project(models.Model):
    PROJECT_TYPE_CHOICES = (
        ('kanban', 'Kanban'),
        ('scrum', 'Scrum'),
    )

    name = models.CharField(max_length=255)
    description = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)

    project_lead = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leading_projects'
    )

    # ✅ NEW (important)
    department = models.CharField(max_length=100, null=True, blank=True)

    # ====== METADATA & CATEGORIZATION ======
    CATEGORY_CHOICES = (
        ('engineering', 'Engineering'),
        ('marketing', 'Marketing'),
        ('sales', 'Sales'),
        ('product', 'Product'),
        ('design', 'Design'),
        ('operations', 'Operations'),
        ('hr', 'HR'),
        ('finance', 'Finance'),
        ('other', 'Other'),
    )
    category = models.CharField(max_length=50, choices=CATEGORY_CHOICES, default='other', null=True, blank=True)
    project_url = models.URLField(max_length=500, null=True, blank=True, help_text='Link to documentation, repo, or external resource')

    # ====== VISUAL IDENTITY ======
    avatar = models.ImageField(upload_to='project_avatars/', null=True, blank=True, help_text='Project icon or logo')

    # ====== TIMELINE & PRIORITY ======
    PRIORITY_CHOICES = (
        ('p1', 'P1 - Critical'),
        ('p2', 'P2 - High'),
        ('p3', 'P3 - Medium'),
        ('p4', 'P4 - Low'),
    )
    start_date = models.DateField(null=True, blank=True)
    target_end_date = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='p3', null=True, blank=True)

    # ====== SECURITY & GOVERNANCE ======
    is_private = models.BooleanField(default=False)
    default_assignee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_projects'
    )

    project_type = models.CharField(
        max_length=10,
        choices=PROJECT_TYPE_CHOICES,
        default='kanban',
    )

    key_prefix = models.CharField(max_length=10, default='TF')
    next_issue_number = models.PositiveIntegerField(default=1)

    members = models.ManyToManyField(
        User,
        through='ProjectMembership',
        related_name='projects'
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        if self.project_lead:
            ProjectMembership.objects.get_or_create(
                user=self.project_lead,
                project=self,
                defaults={'role': 'project_lead'}
            )

    def __str__(self):
        return self.name
    
    
# ---------------- LABEL ----------------
class Label(models.Model):
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7)

    def __str__(self):
        return self.name


# ---------------- TASK ----------------
class Task(models.Model):

    STATUS_CHOICES = [
        ('todo', 'To Do'),
        ('in_progress', 'In Progress'),
        ('in_review', 'In Review'),
        ('qa', 'Send to QA'),
        ('done', 'Done'),
    ]

    ISSUE_TYPE_CHOICES = [
        ('epic', 'Epic'),
        ('story', 'Story'),
        ('task', 'Task'),
        ('bug', 'Bug'),
    ]

    PRIORITY_CHOICES = [
        ('High', 'High'),
        ('Medium', 'Medium'),
        ('Low', 'Low'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField()

    issue_type = models.CharField(
        max_length=10,
        choices=ISSUE_TYPE_CHOICES,
        default='task',
    )

    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='children',
    )

    assigned_to = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='assigned_tasks',
        null=True,
        blank=True
    )

    reporter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='reported_tasks',
        null=True,
        blank=True,
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='created_tasks'
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True
    )

    issue_number = models.PositiveIntegerField(null=True, blank=True, db_index=True)


        # ✅ ADD HERE (CORRECT PLACE)
    team = models.ForeignKey(
        'Team',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks'
    )

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='todo'
    )

    rank = models.FloatField(default=0.0, db_index=True)

    priority = models.CharField(
        max_length=10,
        choices=PRIORITY_CHOICES,
        default='Medium'
    )

    # ✅ FIXED
    labels = models.ManyToManyField('Label', blank=True)

    # Timeline Metadata
    start_date = models.DateField(null=True, blank=True, help_text="Initiated Date (Start)")
    due_date = models.DateField(null=True, blank=True, help_text="Due Date (Deadline)")
    delivery_date = models.DateField(null=True, blank=True, help_text="Delivery Date (Actual Completion)")

    created_at = models.DateTimeField(auto_now_add=True)

    @property
    def issue_key(self) -> str:
        if self.project_id and self.issue_number:
            return f"{self.project.key_prefix}-{self.issue_number}"
        # fallback for legacy rows (or tasks without project)
        prefix = self.project.key_prefix if self.project_id else "TF"
        return f"{prefix}-{self.id}"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.issue_number and self.project:
            with transaction.atomic():
                project = Project.objects.select_for_update().get(pk=self.project.pk)

                self.issue_number = project.next_issue_number
                project.next_issue_number += 1
                project.save(update_fields=["next_issue_number"])

        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['project', 'status', 'rank']),
            models.Index(fields=['assigned_to']),
        ]


# ---------------- COMMENT ----------------
class Comment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="comments")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    text = models.TextField()
    created = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username}: {self.text[:20]}"


# ---------------- ATTACHMENT ----------------
class TaskAttachment(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to="", blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return self.file.name if self.file else "No file"


# ---------------- ACTIVITY ----------------
class TaskActivity(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name="activities")
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    action = models.CharField(max_length=100)
    old_value = models.CharField(max_length=100, blank=True, null=True)
    new_value = models.CharField(max_length=100, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.action}"


# ---------------- INVITE ----------------
# =========================
# TEAM
# =========================

class Team(models.Model):

    TEAM_TYPE_CHOICES = (
        ('development', 'Development'),
        ('qa', 'QA'),
        ('design', 'Design'),
        ('deployment', 'Deployment'),
        ('delivery', 'Delivery'),
        ('support', 'Support'),
    )

    name = models.CharField(
        max_length=120
    )

    slug = models.SlugField(
        max_length=140,
        blank=True
    )

    description = models.TextField(
        blank=True,
        null=True
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='teams'
    )

    lead = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='leading_teams',
        null=True,
        blank=True
    )

    members = models.ManyToManyField(
        User,
        related_name='teams',
        blank=True
    )

    team_type = models.CharField(
        max_length=30,
        choices=TEAM_TYPE_CHOICES,
        default='development'
    )

    avatar = models.ImageField(
        upload_to='team_avatars/',
        null=True,
        blank=True
    )

    color = models.CharField(
        max_length=20,
        default='#4F46E5'
    )

    capacity = models.PositiveIntegerField(
        default=10
    )

    is_active = models.BooleanField(
        default=True
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_teams'
    )

    created_at = models.DateTimeField(
        auto_now_add=True
    )

    updated_at = models.DateTimeField(
        auto_now=True
    )

    class Meta:
        ordering = ['name']

        unique_together = (
            ('project', 'name'),
        )

        indexes = [
            models.Index(fields=['project']),
            models.Index(fields=['lead']),
            models.Index(fields=['team_type']),
            models.Index(fields=['is_active']),
        ]

    def __str__(self):
        return f"{self.project.name} - {self.name}"

    @property
    def total_members(self):
        return self.members.count()

    @property
    def workload(self):
        return self.tasks.exclude(status='done').count()

    @property
    def completed_tasks(self):
        return self.tasks.filter(status='done').count()

    def clean(self):

        from django.core.exceptions import ValidationError

        if self.capacity < 1:
            raise ValidationError("Capacity must be greater than 0")

    def save(self, *args, **kwargs):

        from django.utils.text import slugify

        if not self.slug:
            self.slug = slugify(self.name)

        super().save(*args, **kwargs)

class ProjectInvite(models.Model):
    email = models.EmailField()
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    role = models.CharField(max_length=20)
    token = models.CharField(max_length=100, unique=True)
    team = models.ForeignKey(Team, null=True, blank=True, on_delete=models.SET_NULL)

    used = models.BooleanField(default=False)  # ✅ KEEP THIS
    created_at = models.DateTimeField(auto_now_add=True)

# ---------------- NOTIFICATION ----------------
class Notification(models.Model):
    NOTIFICATION_TYPE_CHOICES = [
        ('task_assigned', 'Task Assigned'),
        ('task_updated', 'Task Updated'),
        ('task_commented', 'Task Commented'),
        ('project_updated', 'Project Updated'),
        ('member_added', 'Member Added'),
        ('member_removed', 'Member Removed'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    notification_type = models.CharField(max_length=30, choices=NOTIFICATION_TYPE_CHOICES)
    title = models.CharField(max_length=255)
    message = models.TextField()
    task = models.ForeignKey(Task, on_delete=models.CASCADE, null=True, blank=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read', '-created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.title}"
    
class RolePermission(models.Model):
    PERMISSION_CHOICES = (
        ('create_task', 'Create Task'),
        ('delete_task', 'Delete Task'),
        ('assign_task', 'Assign Task'),
    )

    role = models.CharField(max_length=25, choices=ProjectMembership.ROLE_CHOICES)
    permission = models.CharField(max_length=50, choices=PERMISSION_CHOICES)


# ---------------- DEPARTMENT PIPELINE SETTINGS ----------------
class DepartmentPipelineSettings(models.Model):
    DEPARTMENT_CHOICES = [
        ('business', 'Business'),
        ('it', 'IT Department'),
        ('marketing', 'Marketing Team'),
        ('design', 'Design Team'),
        ('engineering', 'Engineering'),
        ('product', 'Product'),
        ('operations', 'Operations'),
        ('cross_functional', 'Cross-Functional Team'),
    ]

    STAGE_CHOICES = [
        ('todo', 'Initiated / To Do'),
        ('in_progress', 'Dev / In Progress'),
        ('in_review', 'UI/UX Review'),
        ('qa', 'QA / Testing'),
        ('done', 'Deployment / Done'),
    ]

    department = models.CharField(max_length=50, choices=DEPARTMENT_CHOICES)
    stage_key = models.CharField(max_length=20, choices=STAGE_CHOICES)
    is_visible = models.BooleanField(default=True)
    updated_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='pipeline_setting_updates'
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('department', 'stage_key')]

    def __str__(self):
        return f"{self.get_department_display()} — {self.get_stage_key_display()} ({'on' if self.is_visible else 'off'})"


# ── IT DEPARTMENT ──────────────────────────────────────────────────────────────
class ITDepartment(models.Model):
    name        = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


# ── IT TEAM ────────────────────────────────────────────────────────────────────
class ITTeam(models.Model):
    TEAM_TYPE_CHOICES = [
        ('initiated',   'Initiated / Planning'),
        ('design',      'Design / UI-UX'),
        ('development', 'Development'),
        ('qa',          'QA / Testing'),
        ('devops',      'DevOps / Deployment'),
        ('delivery',    'Delivery'),
    ]

    name       = models.CharField(max_length=150, unique=True)
    department = models.ForeignKey(
        ITDepartment, on_delete=models.CASCADE, related_name='it_teams'
    )
    team_type  = models.CharField(
        max_length=30, choices=TEAM_TYPE_CHOICES, default='development'
    )
    color      = models.CharField(max_length=20, default='#4F46E5')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['department', 'name']

    def __str__(self):
        return f"{self.name} ({self.department.name})"


# ── PROJECT STAGE CONFIGURATION ────────────────────────────────────────────────
class ProjectStageConfiguration(models.Model):
    """
    Maps an ITTeam to a sequence position in the progress tracker.
    project=None  → global default (applies to all projects without a custom config).
    project=<pk>  → project-specific override.
    """
    project       = models.ForeignKey(
        'Project', on_delete=models.CASCADE,
        null=True, blank=True, related_name='stage_configs'
    )
    team          = models.ForeignKey(
        ITTeam, on_delete=models.CASCADE, related_name='stage_configs'
    )
    seq_order     = models.PositiveIntegerField(default=0)
    label_override = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ['project_id', 'seq_order']

    def get_label(self):
        return self.label_override or self.team.name

    def __str__(self):
        scope = self.project.name if self.project_id else 'Global'
        return f"[{scope}] #{self.seq_order} — {self.team.name}"
