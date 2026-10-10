from __future__ import annotations


preview_endpoints_by_capability = {
    "repair_provider_interoperability": "/api/provider-interoperability/repair/preview",
    "repair_provider_interoperability_task": "/api/provider-interoperability/tasks/{thread_id}/repair/preview",
    "rollback_provider_interoperability": "/api/provider-interoperability/rollback/preview",
    "repair_official_thread_tools": "/api/codex/official-thread-tools/repair/preview",
    "show_thread": "/api/threads/{thread_id}/action-preview?action=show",
    "hide_thread": "/api/threads/{thread_id}/action-preview?action=hide",
    "duplicate_thread": "/api/threads/{thread_id}/action-preview?action=duplicate",
    "archive_thread": "/api/threads/{thread_id}/action-preview?action=archive",
    "migrate_thread": "/api/threads/{thread_id}/migrate/preview",
    "move_thread_workspace": "/api/threads/{thread_id}/move-workspace/preview",
    "slim_thread": "/api/threads/{thread_id}/slim/preview",
    "rename_project": "/api/projects/rename/preview",
    "import_thread_from_home": "/api/import/thread/preview",
    "import_project_from_home": "/api/import/project/preview",
    "write_resource": "/api/resources/write/preview",
    "copy_resource_from_home": "/api/resources/copy-from-home/preview",
    "export_portable_backup": "/api/backups/{backup_id}/portable/preview",
    "restore_portable_backup": "/api/backups/{backup_id}/portable/restore/preview",
    "restore_backup": "/api/backups/{backup_id}/restore/preview",
}
