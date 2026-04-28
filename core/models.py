from django.db import models
from django.contrib.auth.models import User


# ---------------- PROFILE ----------------
class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    oauth_provider = models.CharField(max_length=50, blank=True)  # 'google', 'github'
    oauth_id = models.CharField(max_length=255, blank=True, unique=True)
    
    def __str__(self):
        return self.user.username


# ---------------- PROJECT MEMBERSHIP ----------------
class ProjectMembership(models.Model):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('project_lead', 'Project Lead'),
        ('ui_ux_designer', 'UI/UX Designer'),
        ('developer', 'Developer'),
        ('tester', 'Tester'),
        ('qa', 'QA'),
        ('deployment_team', 'Deployment Team'),
        ('delivery_team', 'Delivery Team'),
    )

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    project = models.ForeignKey('Project', on_delete=models.CASCADE)  # ✅ FIXED
    role = models.CharField(max_length=25, choices=ROLE_CHOICES)

    class Meta:
        unique_together = ('user', 'project')

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
class Team(models.Model):
    name = models.CharField(max_length=100)

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='teams'
    )

    lead = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='leading_teams'
    )

    members = models.ManyToManyField(
        User,
        related_name='teams',
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


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
    role = models.CharField(max_length=25, choices=ProjectMembership.ROLE_CHOICES)
    permission = models.CharField(max_length=100)  # 'can_create_task', 'can_delete_user', etc.
    
    

    