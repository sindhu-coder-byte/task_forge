from django.contrib import admin
from .models import Profile, Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    """The only sanctioned task-creation path (the create_task view) requires
    a project — but the model itself allows project=null for legacy-row
    tolerance in issue_key's fallback branch. A bare admin.site.register(Task)
    let admin-created tasks skip that requirement entirely: a projectless
    task falls back to a "TF-{id}" issue key, is invisible on every
    project-scoped board/backlog, and only ever surfaces in global queries
    like the admin dashboard's org-wide overdue list — with no page anywhere
    for anyone to actually manage or resolve it.
    """
    list_display = ("issue_key", "title", "project", "status", "due_date", "is_archived")
    list_filter = ("status", "is_archived")

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.base_fields["project"].required = True
        return form


admin.site.register(Profile)
