from django.contrib import admin
from django.utils.html import format_html
from .models import Profile, Task, SDLCPhase, Department, DepartmentRole


admin.site.register(Profile)
admin.site.register(Task)


# ── SDLC Phase ────────────────────────────────────────────────────────────────

@admin.register(SDLCPhase)
class SDLCPhaseAdmin(admin.ModelAdmin):
    list_display       = ('order', 'key', 'label')
    list_display_links = ('key',)
    list_editable      = ('order', 'label')
    ordering           = ('order',)


# ── Department Role (inline) ───────────────────────────────────────────────────

class DepartmentRoleInline(admin.TabularInline):
    model          = DepartmentRole
    extra          = 1
    fields         = ('name', 'key', 'can_manage_all', 'transitions', 'is_active')
    show_change_link = True


# ── Department ────────────────────────────────────────────────────────────────

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display   = ('name', 'key', 'sdlc_phase_badges', 'role_count', 'is_active', 'created_at')
    list_filter    = ('is_active', 'sdlc_phases')
    search_fields  = ('name', 'key')
    filter_horizontal = ('sdlc_phases',)
    readonly_fields   = ('created_at',)
    inlines        = [DepartmentRoleInline]
    prepopulated_fields = {'key': ('name',)}

    def sdlc_phase_badges(self, obj):
        phases = obj.sdlc_phases.all()
        if not phases:
            return '—'
        badges = ' '.join(
            f'<span style="background:#2563eb;color:#fff;padding:2px 6px;'
            f'border-radius:4px;font-size:11px;margin-right:2px">{p.label}</span>'
            for p in phases
        )
        return format_html(badges)
    sdlc_phase_badges.short_description = 'SDLC Phases'

    def role_count(self, obj):
        return obj.roles.filter(is_active=True).count()
    role_count.short_description = 'Active Roles'


# ── DepartmentRole (standalone) ────────────────────────────────────────────────

@admin.register(DepartmentRole)
class DepartmentRoleAdmin(admin.ModelAdmin):
    list_display  = ('name', 'key', 'department', 'can_manage_all', 'transition_summary', 'is_active')
    list_filter   = ('department', 'can_manage_all', 'is_active')
    search_fields = ('name', 'key', 'department__name')
    list_editable = ('is_active',)
    prepopulated_fields = {'key': ('name',)}

    fieldsets = (
        ('Identity', {
            'fields': ('department', 'name', 'key', 'is_active'),
        }),
        ('Workflow Permissions', {
            'description': (
                'Set transitions as a JSON dict: {"todo": ["in_progress"], '
                '"in_progress": ["qa", "in_review"]}. '
                'Valid status keys: todo, in_progress, in_review, qa, done. '
                'Or enable "Can manage all" to grant full access.'
            ),
            'fields': ('can_manage_all', 'transitions'),
        }),
    )

    def transition_summary(self, obj):
        if obj.can_manage_all:
            return format_html('<span style="color:green;font-weight:bold">Full Access</span>')
        data = obj.transitions or {}
        if not data:
            return '—'
        parts = [f'{k} → {", ".join(v)}' for k, v in data.items()]
        return ' | '.join(parts)
    transition_summary.short_description = 'Transitions'
