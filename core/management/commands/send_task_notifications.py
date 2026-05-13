from django.core.management.base import BaseCommand, CommandError

from core.notifications import check_due_dates, check_due_today, check_overdue_tasks, send_project_digests


class Command(BaseCommand):
    help = (
        "Send automated workflow emails for approaching due dates and missed deadlines. "
        "By default checks tasks due in 3 days (assignee) and 1 day (assignee + lead)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days-before",
            default="3,1",
            help=(
                "Comma-separated list of day windows to check, e.g. '3,1'. "
                "Each window applies its own recipient rules. Default: '3,1'."
            ),
        )
        parser.add_argument(
            "--skip-due-soon",
            action="store_true",
            help="Skip approaching due-date notifications.",
        )
        parser.add_argument(
            "--skip-overdue",
            action="store_true",
            help="Skip missed-deadline notifications.",
        )
        parser.add_argument(
            "--skip-due-today",
            action="store_true",
            help="Skip 'due today' alerts (tasks whose due date is today).",
        )
        parser.add_argument(
            "--skip-digest",
            action="store_true",
            help="Skip project progress digest emails to project leads.",
        )

    def handle(self, *args, **options):
        # Parse comma-separated days list
        try:
            days_list = [int(d.strip()) for d in options["days_before"].split(",") if d.strip()]
        except ValueError:
            raise CommandError("--days-before must be comma-separated integers, e.g. '3,1'.")

        due_today_checked = 0
        due_checked = 0
        overdue_checked = 0

        if not options["skip_due_today"]:
            due_today_checked = check_due_today()
            self.stdout.write(f"  Due today: {due_today_checked} task(s) processed.")

        if not options["skip_due_soon"]:
            for days in days_list:
                count = check_due_dates(days_before=days)
                due_checked += count
                self.stdout.write(f"  Due in {days}d: {count} task(s) processed.")

        if not options["skip_overdue"]:
            overdue_checked = check_overdue_tasks()

        digest_sent = 0
        if not options["skip_digest"]:
            digest_sent = send_project_digests()
            self.stdout.write(f"  Digest: {digest_sent} project(s) processed.")

        self.stdout.write(
            self.style.SUCCESS(
                f"Sweep complete — due-today: {due_today_checked} task(s), "
                f"due-soon: {due_checked} task(s), "
                f"overdue: {overdue_checked} task(s), "
                f"digest: {digest_sent} project(s) processed."
            )
        )
