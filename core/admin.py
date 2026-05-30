from django.contrib import admin
from .models import Profile, Task, Project, ProjectMilestone


admin.site.register(Profile)
admin.site.register(Task)


# ── ProjectMilestone inline on Project ───────────────────────────────────────

class ProjectMilestoneInline(admin.TabularInline):
    model  = ProjectMilestone
    extra  = 1
    fields = ('order', 'name', 'icon', 'color', 'role_key', 'is_active')
    ordering = ('order',)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ('name', 'department', 'project_lead', 'created_by', 'milestone_count')
    search_fields = ('name', 'department')
    inlines = [ProjectMilestoneInline]

    def milestone_count(self, obj):
        return obj.milestones.filter(is_active=True).count()
    milestone_count.short_description = 'Milestones'


@admin.register(ProjectMilestone)
class ProjectMilestoneAdmin(admin.ModelAdmin):
    list_display  = ('name', 'project', 'order', 'icon', 'color', 'role_key', 'is_active')
    list_filter   = ('project', 'is_active')
    list_editable = ('order', 'is_active')
    search_fields = ('name', 'project__name', 'role_key')
    ordering      = ('project', 'order')
