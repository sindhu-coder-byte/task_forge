from django.contrib import admin
from django.utils.html import format_html
from .models import Profile, Task, Department, DepartmentRole


admin.site.register(Profile)
admin.site.register(Task)


# ── Department Role (inline inside Department) ────────────────────────────────

class DepartmentRoleInline(admin.TabularInline):
    model  = DepartmentRole
    extra  = 1
    fields = (
        'name', 'key',
        'can_start_task', 'can_submit_task', 'can_send_to_qa',
        'can_approve_task', 'can_reject_task', 'can_manage_all',
        'is_active',
    )
    prepopulated_fields = {'key': ('name',)}
    show_change_link    = True


# ── Department ────────────────────────────────────────────────────────────────

@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display        = ('name', 'key', 'sdlc_phase_badge', 'role_count', 'is_active', 'created_at')
    list_filter         = ('sdlc_phase', 'is_active')
    search_fields       = ('name', 'key')
    readonly_fields     = ('created_at',)
    prepopulated_fields = {'key': ('name',)}
    inlines             = [DepartmentRoleInline]

    fieldsets = (
        ('Department Identity', {
            'fields': ('name', 'key', 'is_active'),
        }),
        ('SDLC Lifecycle', {
            'description': 'Select the primary SDLC stage this department operates in.',
            'fields': ('sdlc_phase',),
        }),
        ('Meta', {
            'fields': ('created_at',),
            'classes': ('collapse',),
        }),
    )

    def sdlc_phase_badge(self, obj):
        colours = {
            'requirements': ('#dbeafe', '#1e40af'),
            'design':       ('#fdf4ff', '#86198f'),
            'development':  ('#dcfce7', '#15803d'),
            'testing':      ('#fef3c7', '#92400e'),
            'deployment':   ('#d1fae5', '#065f46'),
            'monitoring':   ('#e0f2fe', '#0369a1'),
            'cross_phase':  ('#f1f5f9', '#475569'),
        }
        bg, fg = colours.get(obj.sdlc_phase, ('#f1f5f9', '#475569'))
        return format_html(
            '<span style="background:{};color:{};padding:3px 10px;'
            'border-radius:999px;font-size:11px;font-weight:700;">{}</span>',
            bg, fg, obj.get_sdlc_phase_display(),
        )
    sdlc_phase_badge.short_description = 'SDLC Phase'

    def role_count(self, obj):
        return obj.roles.filter(is_active=True).count()
    role_count.short_description = 'Active Roles'


# ── Department Role (standalone list) ─────────────────────────────────────────

@admin.register(DepartmentRole)
class DepartmentRoleAdmin(admin.ModelAdmin):
    list_display  = (
        'name', 'key', 'department',
        'can_start_task', 'can_submit_task', 'can_send_to_qa',
        'can_approve_task', 'can_reject_task', 'can_manage_all',
        'is_active',
    )
    list_filter   = ('department', 'can_manage_all', 'is_active')
    list_editable = ('is_active',)
    search_fields = ('name', 'key', 'department__name')
    prepopulated_fields = {'key': ('name',)}

    fieldsets = (
        ('Role Identity', {
            'fields': ('department', 'name', 'key', 'is_active'),
        }),
        ('Workflow Permissions', {
            'description': (
                'Tick the task actions this role is allowed to perform. '
                'Enable "Full access" to skip all individual checks.'
            ),
            'fields': (
                'can_manage_all',
                'can_start_task',
                'can_submit_task',
                'can_send_to_qa',
                'can_approve_task',
                'can_reject_task',
            ),
        }),
    )
