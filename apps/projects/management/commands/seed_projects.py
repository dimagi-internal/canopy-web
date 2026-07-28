from django.core.management.base import BaseCommand
from apps.projects.models import Project

PROJECTS = [
    {"name": "canopy-web", "slug": "canopy-web", "repo_url": "https://github.com/dimagi-internal/canopy-web", "deploy_url": "https://canopy-web-502453377792.us-central1.run.app", "visibility": "public", "status": "active"},
    {"name": "ace", "slug": "ace", "repo_url": "https://github.com/jjackson/ace", "visibility": "public", "status": "active"},
    {"name": "ace-web", "slug": "ace-web", "repo_url": "https://github.com/jjackson/ace-web", "deploy_url": "https://labs.connect.dimagi.com/ace", "visibility": "public", "status": "active"},
    {"name": "commcare-ios", "slug": "commcare-ios", "repo_url": "https://github.com/jjackson/commcare-ios", "visibility": "public", "status": "active"},
    {"name": "connect-search", "slug": "connect-search", "repo_url": "https://github.com/jjackson/connect-search", "deploy_url": "https://connect-search-frontend-502453377792.us-central1.run.app", "visibility": "private", "status": "active"},
    {"name": "connect-website", "slug": "connect-website", "repo_url": "https://github.com/jjackson/connect-website", "deploy_url": "https://jjackson.github.io/connect-website/overview/", "visibility": "public", "status": "active"},
    {"name": "canopy", "slug": "canopy", "repo_url": "https://github.com/dimagi-internal/canopy", "visibility": "private", "status": "active"},
    {"name": "connect-labs", "slug": "connect-labs", "repo_url": "https://github.com/jjackson/connect-labs", "deploy_url": "https://labs.connect.dimagi.com", "visibility": "public", "status": "active"},
    {"name": "chrome-sales", "slug": "chrome-sales", "repo_url": "https://github.com/jjackson/chrome-sales", "visibility": "private", "status": "active"},
    {"name": "scout", "slug": "scout", "repo_url": "https://github.com/jjackson/scout", "visibility": "public", "status": "active"},
    {"name": "canopy-skills", "slug": "canopy-skills", "repo_url": "https://github.com/dimagi-internal/canopy-skills", "visibility": "public", "status": "active"},
    {"name": "reef", "slug": "reef", "repo_url": "https://github.com/jjackson/reef", "visibility": "public", "status": "archived"},
    {"name": "commcare-connect", "slug": "commcare-connect", "repo_url": "https://github.com/jjackson/commcare-connect", "visibility": "public", "status": "active"},
]

class Command(BaseCommand):
    help = "Seed database with the initial 13 projects"

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete all projects first")

    def handle(self, *args, **options):
        if options["reset"]:
            count = Project.objects.count()
            Project.objects.all().delete()
            self.stdout.write(f"Deleted {count} projects.")

        created = 0
        skipped = 0
        for spec in PROJECTS:
            _, was_created = Project.objects.get_or_create(
                slug=spec["slug"],
                defaults={
                    "name": spec["name"],
                    "repo_url": spec.get("repo_url", ""),
                    "deploy_url": spec.get("deploy_url", ""),
                    "visibility": spec.get("visibility", "public"),
                    "status": spec.get("status", "active"),
                },
            )
            if was_created:
                created += 1
            else:
                skipped += 1

        self.stdout.write(self.style.SUCCESS(f"Seeded projects: {created} created, {skipped} already existed."))
