"""Downloader module for downloading files from GoFile.

Example usage:
    python3 gofile_downloader.py <album_url>
    python3 gofile_downloader.py <album_url> <password>
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import requests

from src.config import (
    DEFAULT_USAGE,
    DOWNLOAD_FOLDER,
    EXTENDED_HEADERS,
    LOCALE,
    MAX_WORKERS,
    PASSWORD_USAGE,
    parse_arguments,
)
from src.download_utils import save_file_with_progress
from src.file_utils import create_download_directory
from src.general_utils import clear_terminal
from src.gofile_utils import (
    check_response_status,
    generate_content_url,
    generate_website_token,
    get_account_token,
    get_content_id,
)
from src.managers.live_manager import LiveManager, initialize_managers

if TYPE_CHECKING:
    from argparse import Namespace

DEFAULT_DOWNLOAD_PATH = Path.cwd() / DOWNLOAD_FOLDER


class Downloader:
    """Class to handle downloading files from a specified URL in parallel.

    It manages the download process, including handling authentication, partial
    downloads, and error checking. This class supports resuming interrupted downloads,
    verifying file integrity, and organizing downloads into appropriate directories.
    """

    def __init__(
        self,
        url: str,
        live_manager: LiveManager,
        args: Namespace | None = None,
    ) -> None:
        """Initialize the downloader with the given parameters."""
        self.url = url
        self.live_manager = live_manager
        self.password = getattr(args, "password", None)
        self.token = get_account_token()
        self.selection_mode = getattr(args, "selection_mode", "interactive")
        self.selection_query = getattr(args, "selection", None)
        custom_path = getattr(args, "custom_path", None)

        self.download_path = (
            Path(custom_path)
            if custom_path is not None
            else DEFAULT_DOWNLOAD_PATH
        )
        self.download_path.mkdir(parents=True, exist_ok=True)
        os.chdir(self.download_path)

    def download_item(self, current_task: int, file_info: dict) -> None:
        """Download a single file."""
        filename = file_info["filename"]
        final_path = Path(file_info["download_path"]) / filename
        download_link = file_info["download_link"]

        # Skip file if it already exists and is not empty
        if Path(final_path).exists():
            self.live_manager.update_log(
                event="Skipped download",
                details=f"{filename} has already been downloaded.",
            )
            return

        headers = self._prepare_headers(url=download_link)

        # Perform the download and handle possible errors
        with requests.get(
            download_link,
            headers=headers,
            stream=True,
            timeout=(10, 30),
        ) as response:
            if not check_response_status(response, filename):
                return

            task_id = self.live_manager.add_task(current_task=current_task)
            save_file_with_progress(response, final_path, task_id, self.live_manager)

    def run_in_parallel(self, content_directory: str, files_info: list[dict]) -> None:
        """Execute the file downloads in parallel."""
        os.chdir(content_directory)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            for current_task, item_info in enumerate(files_info):
                executor.submit(self.download_item, current_task, item_info)

        os.chdir(self.download_path)

    def _prepare_headers(
        self,
        url: str | None = None,
        *,
        include_auth: bool = False,
    ) -> dict:
        """Prepare the HTTP headers for the request."""
        # Base headers common for all requests
        headers = EXTENDED_HEADERS

        # Add authentication headers if required
        if include_auth:
            headers["Authorization"] = f"Bearer {self.token}"
            headers["X-Website-Token"] = generate_website_token(self.token)
            headers["X-BL"] = LOCALE

        else:
            # Add Referer and Origin headers if URL is provided
            if url:
                adjusted_url = url + ("/" if not url.endswith("/") else "")
                headers["Referer"] = adjusted_url
                headers["Origin"] = url

            # Add Cookie header when URL is not needed
            headers["Cookie"] = f"accountToken={self.token}"

        return headers

    def parse_links(
        self,
        identifier: str,
        current_path: Path,
        password: str | None = None,
    ) -> dict | None:
        """Parse content and return a tree structure for files and folders."""

        def check_password(data: dict) -> bool:
            password_exists = "password" in data
            password_status_ok = data.get("passwordStatus") == "passwordOk"
            return password_exists and not password_status_ok

        content_url = generate_content_url(identifier, password=password)
        headers = self._prepare_headers(include_auth=True)

        response = requests.get(content_url, headers=headers, timeout=10).json()
        if response["status"] != "ok":
            self.live_manager.update_log(
                event="Failed request",
                details=f"Failed to get a link as response from {content_url}.",
            )
            return None

        data = response["data"]
        if check_password(data):
            self.live_manager.update_log(
                event="Missing password",
                details="The URL requires a valid password. "
                "Please provide one to proceed.",
            )
            return None

        # Handle folder
        if data["type"] == "folder":
            folder_path = current_path / data["name"]
            children = []
            api_children = list(data["children"].values())
            files_by_creation_date = self._sort_files_by_creation_date(api_children)
            sorted_files_iter = iter(files_by_creation_date)

            for child in api_children:
                if child["type"] != "folder":
                    child = next(sorted_files_iter)

                if child["type"] == "folder":
                    child_content = self.parse_links(
                        child["id"],
                        folder_path,
                        password,
                    )
                    if child_content:
                        children.append(child_content)
                else:
                    children.append(
                        {
                            "type": "file",
                            "name": child["name"],
                            "relative_path": str(folder_path / child["name"]),
                            "download_link": child["link"],
                        },
                    )
            return {
                "type": "folder",
                "name": data["name"],
                "relative_path": str(folder_path),
                "children": children,
            }

        # Handle file
        return {
            "type": "file",
            "name": data["name"],
            "relative_path": str(current_path / data["name"]),
            "download_link": data["link"],
        }

    @staticmethod
    def _get_creation_timestamp(item: dict) -> float | None:
        """Extract an item's creation timestamp from GoFile fields if available."""
        for key in ("createTime", "create_time", "createdAt", "created_at"):
            value = item.get(key)
            if value in (None, ""):
                continue

            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str):
                stripped = value.strip()
                if not stripped:
                    continue
                try:
                    return float(stripped)
                except ValueError:
                    try:
                        return datetime.fromisoformat(
                            stripped.replace("Z", "+00:00"),
                        ).timestamp()
                    except ValueError:
                        continue
        return None

    def _sort_files_by_creation_date(self, items: list[dict]) -> list[dict]:
        """Sort files by creation date (oldest first) with stable API-order fallback."""
        files_with_indexes = [
            (index, item)
            for index, item in enumerate(items)
            if item["type"] != "folder"
        ]
        sortable_files = [
            (index, item, self._get_creation_timestamp(item))
            for index, item in files_with_indexes
        ]
        return [
            item
            for _, item, _ in sorted(
                sortable_files,
                key=lambda indexed_item: (
                    indexed_item[2] is None,
                    indexed_item[2] or 0.0,
                    indexed_item[0],
                ),
            )
        ]

    def _build_file_list(self, content_item: dict) -> list[dict]:
        """Flatten a content tree into downloadable file metadata."""
        if content_item["type"] == "file":
            return [
                {
                    "relative_path": content_item["relative_path"],
                    "download_link": content_item["download_link"],
                },
            ]

        files_info = []
        for child in content_item["children"]:
            files_info.extend(self._build_file_list(child))
        return files_info

    def _build_selectable_items(
        self,
        content_item: dict,
        all_files: list[dict],
    ) -> list[dict]:
        """Build selectable items for interactive file/folder selection."""
        selectable_items = []
        file_paths = {
            file_info["relative_path"]
            for file_info in all_files
        }

        def traverse(item: dict, is_root: bool = False) -> None:
            if item["type"] == "folder":
                folder_path = item["relative_path"]
                selected_paths = {
                    path
                    for path in file_paths
                    if path.startswith(f"{folder_path}/")
                }
                if selected_paths and not is_root:
                    selectable_items.append(
                        {
                            "type": "folder",
                            "relative_path": folder_path,
                            "selected_paths": selected_paths,
                        },
                    )
                for child in item["children"]:
                    traverse(child)
            else:
                file_path = item["relative_path"]
                selectable_items.append(
                    {
                        "type": "file",
                        "relative_path": file_path,
                        "selected_paths": {file_path},
                        "download_link": item["download_link"],
                    },
                )

        traverse(content_item, is_root=True)
        return selectable_items

    @staticmethod
    def _parse_selection_expression(expression: str, max_items: int) -> set[int]:
        """Parse selection input like '1,3,5' or '1-5' into indexes (1-based)."""
        selection = expression.strip().lower()
        if selection == "all":
            return set(range(1, max_items + 1))
        if selection == "none":
            return set()

        selected_indexes = set()
        for segment in selection.split(","):
            segment = segment.strip()
            if not segment:
                continue
            if "-" in segment:
                start_text, end_text = segment.split("-", maxsplit=1)
                start = int(start_text.strip())
                end = int(end_text.strip())
                if start > end:
                    start, end = end, start
                selected_indexes.update(range(start, end + 1))
            else:
                selected_indexes.add(int(segment))

        invalid_indexes = {
            value
            for value in selected_indexes
            if value < 1 or value > max_items
        }
        if invalid_indexes:
            raise ValueError(f"Invalid selection index(es): {sorted(invalid_indexes)}")

        return selected_indexes

    def _select_files_from_items(self, selectable_items: list[dict]) -> list[dict]:
        """Select files from parsed content based on CLI mode and/or user input."""
        total_items = len(selectable_items)
        if total_items == 0:
            return []

        if self.selection_mode == "all":
            selected_indexes = set(range(1, total_items + 1))
        elif self.selection_mode == "none":
            selected_indexes = set()
        elif self.selection_query:
            try:
                selected_indexes = self._parse_selection_expression(
                    self.selection_query,
                    total_items,
                )
            except ValueError:
                self.live_manager.update_log(
                    event="Invalid selection",
                    details=f"Invalid --selection value: {self.selection_query}",
                )
                return []
        else:
            console = self.live_manager.live.console
            console.print("\nAvailable items to download:")
            for index, item in enumerate(selectable_items, start=1):
                item_type = "Folder" if item["type"] == "folder" else "File"
                console.print(f"{index:>3}. [{item_type}] {item['relative_path']}")
            console.print(
                "\nChoose items (examples: '1,3,5', '1-5', 'all', 'none').",
            )

            while True:
                selection_input = console.input("> ").strip()
                try:
                    selected_indexes = self._parse_selection_expression(
                        selection_input,
                        total_items,
                    )
                    break
                except ValueError:
                    console.print("Invalid selection. Please try again.")

        selected_paths = set()
        for selected_index in selected_indexes:
            selected_paths.update(
                selectable_items[selected_index - 1]["selected_paths"],
            )

        return [
            item
            for item in selectable_items
            if item["type"] == "file"
            and item["relative_path"] in selected_paths
        ]

    def initialize_download(self) -> None:
        """Initialize the download process."""
        content_id = get_content_id(self.url)
        content_directory = self.download_path / content_id
        create_download_directory(content_directory)

        hashed_password = (
            hashlib.sha256(self.password.encode()).hexdigest()
            if self.password
            else self.password
        )
        content_tree = self.parse_links(content_id, Path(""), hashed_password)
        if not content_tree:
            if not os.listdir(content_directory):
                Path(content_directory).rmdir()
            return

        all_files_info = self._build_file_list(content_tree)
        if content_tree["type"] == "folder":
            selectable_items = self._build_selectable_items(content_tree, all_files_info)
            selected_files = self._select_files_from_items(selectable_items)
        else:
            selected_files = all_files_info

        files_info = []
        for file_info in selected_files:
            file_path = Path(file_info["relative_path"])
            files_info.append(
                {
                    "download_path": str(content_directory / file_path.parent),
                    "filename": file_path.name,
                    "download_link": file_info["download_link"],
                },
            )

        for file_info in files_info:
            create_download_directory(file_info["download_path"])

        # Remove the root content directory if there's no file or subdirectory.
        if not os.listdir(content_directory) and not files_info:
            Path(content_directory).rmdir()
            return

        self.live_manager.add_overall_task(
            description=content_id,
            num_tasks=len(files_info),
        )
        self.run_in_parallel(content_directory, files_info)


def handle_download_process(
    url: str,
    live_manager: LiveManager,
    args: Namespace | None = None,
) -> None:
    """Handle the process of downloading content from a specified URL."""
    if url is None:
        logging.error(
            "Default usage: %s\nPassword usage: %s\n",
            DEFAULT_USAGE,
            PASSWORD_USAGE,
        )
        sys.exit(1)

    downloader = Downloader(url=url, live_manager=live_manager, args=args)
    downloader.initialize_download()


def main() -> None:
    """Process command-line arguments to download an album from a specified URL."""
    clear_terminal()
    args = parse_arguments()
    live_manager = initialize_managers()

    try:
        with live_manager.live:
            handle_download_process(args.url, live_manager, args=args)
            live_manager.stop()

    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
