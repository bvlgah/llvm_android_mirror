#!/usr/bin/env python3
#
# Copyright (C) 2020 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import annotations
import argparse
import collections
import copy
import dataclasses
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.request

import context
from llvm_android.android_version import get_svn_revision_number
from merge_from_upstream import fetch_upstream, sha_to_revision
from llvm_android import paths, source_manager
from llvm_android.utils import check_call, check_output


def parse_args():
    parser = argparse.ArgumentParser(description="Cherry pick upstream LLVM patches.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--sha', nargs='+', help='sha of patches to cherry pick')
    parser.add_argument('--patch-file', help='Use custom patch file for --sha')
    parser.add_argument('--pr', help='Cherry pick from a GitHub PR, e.g., 84422')
    parser.add_argument(
        '--start-version', default='llvm',
        help="""svn revision to start applying patches. 'llvm' can also be used.""")
    parser.add_argument('--bug', help='bug to reference in CLs created (if any)')
    parser.add_argument('--reason', help='issue/reason to mention in CL subject line')
    parser.add_argument('--verbose', help='Enable logging')
    parser.add_argument('--no-verify-merge', action='store_true',
                        help='check if patches can be applied cleanly')
    parser.add_argument('--no-create-cl', action='store_true', help='create a CL')
    args = parser.parse_args()
    return args


def parse_start_version(start_version: str) -> int:
    if start_version == 'llvm':
        return int(get_svn_revision_number())
    m = re.match(r'r?(\d+)', start_version)
    assert m, f'invalid start_version: {start_version}'
    return int(m.group(1))


@dataclass
class PatchItem:
    metadata: Dict[str, Any]
    # metadata['info']: Optional[List[str]]
    # metadata['title']: str
    platforms: List[str]
    rel_patch_path: str
    version_range: Dict[str, Optional[int]]
    # version_range['from']: Optional[int]
    # version_range['until']: Optional[int]

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> PatchItem:
        return PatchItem(
            metadata=d['metadata'],
            platforms=d['platforms'],
            rel_patch_path=d['rel_patch_path'],
            version_range=d['version_range'])

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=collections.OrderedDict)

    @property
    def title(self) -> str:
        return self.metadata['title']

    @property
    def is_local_patch(self) -> bool:
        return not self.rel_patch_path.startswith('cherry/')

    @property
    def sha(self) -> str:
        m = re.match(r'cherry/(.+)\.patch', self.rel_patch_path)
        assert m, self.rel_patch_path
        return m.group(1)

    @property
    def pr_link(self) -> str:
        for line in open(f'patches/{self.rel_patch_path}'):
            if m := re.match(r'Pull Request: (.+)', line):
                return m.group(1)
        raise Exception(f'No PR link found in: {self.rel_patch_path}')

    @property
    def end_version(self) -> Optional[int]:
        return self.version_range.get('until', None)

    @property
    def start_version(self) -> Optional[int]:
        return self.version_range.get('from', None)

    @property
    def sort_key(item: PatchItem) -> Tuple:
        # Keep local patches at the end of the list, and don't change the
        # relative order between two local patches.
        if item.is_local_patch:
            return (True,)

        # Just before local patches, include patches with no end_version. Sort
        # them by start_version.
        if item.end_version is None:
            return (False, math.inf, item.start_version)

        # At the front of the list, sort upstream patches by ascending order of
        # end_version. Don't reorder patches with the same end_version.
        return (False, item.end_version)

    def __lt__(self, other: PatchItem) -> bool:
        """Used to sort patches in PatchList"""
        return self.sort_key < other.sort_key


class PatchList(list):
    """ a list of PatchItem """

    JSON_FILE_PATH = paths.SCRIPTS_DIR / 'patches' / 'PATCHES.json'

    @classmethod
    def load_from_file(cls) -> PatchList:
        with open(cls.JSON_FILE_PATH, 'r') as fh:
            array = json.load(fh)
        return PatchList(PatchItem.from_dict(d) for d in array)

    def save_to_file(self):
        array = [patch.to_dict() for patch in self]
        with open(self.JSON_FILE_PATH, 'w') as fh:
            json.dump(array, fh, indent=4, separators=(',', ': '), sort_keys=True)
            fh.write('\n')

    def check_patches(self) -> bool:
        patch_files = set()
        for patch in self:
            if not patch.start_version:
                logging.error(f"'{patch.title}' doesn't have a start version")
                return False
            if patch.end_version and patch.end_version <= patch.start_version:
                logging.error(
                    f"'{patch.title}' has end version <= start version. It should be removed.")
                return False
            patch_file = paths.SCRIPTS_DIR / 'patches' / patch.rel_patch_path
            if not patch_file.is_file():
                logging.error(f"{patch_file} used by '{patch.title}' isn't a file")
                return False
            if patch_file in patch_files:
                logging.error(f"{patch_file} is used by multiple patches")
                return False
            patch_files.add(patch_file)
        return True


def create_patches_for_sha_list(sha_list: List[str], start_version: int, patch_list: PatchList
                                ) -> PatchList:
    """ generate upstream cherry-pick patch files """
    upstream_dir = paths.TOOLCHAIN_LLVM_PATH
    fetch_upstream()
    result = PatchList()
    for sha in sha_list:
        if len(sha) < 40:
            sha = get_full_sha(upstream_dir, sha)
        version = find_version(sha, patch_list, start_version)
        version_name = '' if version == 1 else f'-v{version}'
        rel_patch_path = f'cherry/{sha}' + version_name + '.patch'
        file_path = paths.SCRIPTS_DIR / 'patches' / rel_patch_path
        with open(file_path, 'w') as fh:
            check_call(f'git format-patch -1 {sha} --stdout',
                       stdout=fh, shell=True, cwd=upstream_dir)

        commit_subject = check_output(
            f'git log -n1 --format=%s {sha}', shell=True, cwd=upstream_dir)
        info: Optional[List[str]] = []
        title = '[UPSTREAM] ' + commit_subject.strip()
        end_version = sha_to_revision(sha)
        metadata = {'info': info, 'title': title}
        platforms = ['android']
        version_range: Dict[str, Optional[int]] = {
            'from': start_version,
            'until': end_version,
        }
        result.append(PatchItem(metadata, platforms, rel_patch_path, version_range))
    return result


def create_patch_for_sha_with_patch_file(patch_file: Path, sha: str, start_version: int,
                                         patch_list: PatchList) -> PatchList:
    """ add patch files for sha"""
    upstream_dir = paths.TOOLCHAIN_LLVM_PATH
    result = []
    assert len(sha) >= 40, f'the length of {sha} is {len(sha)} and it is shorter than 40'
    version = find_version(sha, patch_list, start_version)
    version_name = '' if version == 1 else f'-v{version}'
    rel_patch_path = f'cherry/{sha}' + version_name + '.patch'
    file_path = paths.SCRIPTS_DIR / 'patches' / rel_patch_path
    with open(patch_file, 'r') as source, open(file_path, 'w') as dest:
        for line in source:
            dest.write(line)

    commit_subject = check_output(
        f'git log -n1 --format=%s {sha}', shell=True, cwd=upstream_dir)
    info: Optional[List[str]] = []
    title = '[UPSTREAM] ' + commit_subject.strip()
    end_version = sha_to_revision(sha)
    metadata = {'info': info, 'title': title}
    platforms = ['android']
    version_range: Dict[str, Optional[int]] = {
        'from': start_version,
        'until': end_version,
    }
    result.append(PatchItem(metadata, platforms, rel_patch_path, version_range))
    return result


def get_full_sha(upstream_dir: Path, short_sha: str) -> str:
    return check_output(['git', 'rev-parse', short_sha], cwd=upstream_dir).strip()


def create_cl(new_patches: PatchList, reason: str, bug: Optional[str], cherry: bool):
    file_list = [
        str(paths.SCRIPTS_DIR / 'patches' / p.rel_patch_path) for p in new_patches
    ]

    file_list += ['patches/PATCHES.json']
    check_call(['git', 'add'] + file_list)

    subject = f'[patches] Cherry pick CLS for: {reason}'
    commit_lines = [subject, '']
    script = os.path.basename(sys.argv[0])
    argv_deepcopy = copy.deepcopy(sys.argv[1:])
    for i in range(len(argv_deepcopy)):
        element = argv_deepcopy[i]
        if element.startswith('--reason'):
            del argv_deepcopy[i]
            if element == '--reason':
                del argv_deepcopy[i]
            break

    for patch in new_patches:
        if cherry:  # Add SHA and title for each cherry-pick.
            sha = patch.sha[:11]
            subject = patch.metadata['title']
            if subject.startswith('[UPSTREAM] '):
                subject = subject[len('[UPSTREAM] '):]
            commit_line = sha + ' ' + subject
        else:  # Add link to differential revision.
            commit_line = patch.pr_link
        commit_lines.append(commit_line)
    commit_lines.append('')

    args = ' '.join(argv_deepcopy)
    auto_msg = f'This change is generated automatically by the script:\n  {script} {args}'
    commit_lines += [auto_msg, '']
    if bug:
        if bug.isnumeric():
            commit_lines += [f'Bug: http://b/{bug}', '']
        else:
            commit_lines += [f'Bug: {bug}', '']

    commit_lines += ['', 'Test: N/A']
    check_call(['git', 'commit', '-m', '\n'.join(commit_lines)])


def create_patch_for_pr(pr: str, start_version: int) -> PatchList:
    pr_url = f'https://api.github.com/repos/llvm/llvm-project/pulls/{pr}'
    patch_url_req = urllib.request.Request(f'https://github.com/llvm/llvm-project/pull/{pr}.patch',
                                           method="HEAD")
    patch_url = urllib.request.urlopen(patch_url_req).url

    # TODO: Add commit body and author details as well.
    with urllib.request.urlopen(pr_url) as response:
        data = json.load(response)
    title = data['title']
    assert title, f'Title not found for {pr}'
    print(f'Creating a patch for {title}')
    file_name = f'{pr}.patch'
    abs_file_name = paths.SCRIPTS_DIR / 'patches' / file_name
    # Download the file from `patch_url` and save in `abs_file_name`:
    urllib.request.urlretrieve(patch_url, abs_file_name)
    # Add link to Differential Revision
    link_line = f'Pull Request: {patch_url}\n'
    data = abs_file_name.read_text()
    dash_pos = data.find('\n---')
    assert dash_pos != -1
    dash_pos += 1
    data = data[:dash_pos] + link_line + data[dash_pos:]
    abs_file_name.write_text(data)

    # Extend the PATCHES.json
    result = PatchList()
    info: Optional[List[str]] = []
    rel_patch_path = f'{file_name}'
    end_version = None
    metadata = {'info': info, 'title': title}
    platforms = ['android']
    version_range: Dict[str, Optional[int]] = {
        'from': start_version,
        'until': end_version,
    }
    result.append(PatchItem(metadata, platforms, rel_patch_path, version_range))
    return result


def find_version(sha, patch_list, start_version) -> int:
    """ Return the next version for the given SHA and update end_revision if needed"""
    target = f'cherry/{sha}'
    last_idx = -1
    version = 1
    name = ''

    # Find the latest version
    for i, item in enumerate(patch_list):
        if item.rel_patch_path.startswith(target):
            last_idx = i
            name = item.rel_patch_path.removesuffix('.patch')

    # If this patch is not new, update the end_revision for Vn
    if last_idx != -1:
        patch_list[last_idx].version_range['until'] = start_version
        if name == target:
            return 2
        prefix = target + f'-v'
        version = int(name.removeprefix(prefix)) + 1

    return version


def main() -> bool:
    args = parse_args()
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level)
    patch_list = PatchList.load_from_file()

    assert not (bool(args.sha) and bool(args.pr)), (
        'Only one of cherry-pick or patch supported.'
    )
    if args.pr:
        start_version = parse_start_version(args.start_version)
        new_patches = create_patch_for_pr(args.pr, start_version)
        patch_list.extend(new_patches)
    elif args.sha:
        start_version = parse_start_version(args.start_version)
        if args.patch_file:
            assert len(
                args.sha) == 1,  f'error: --patch-file only requires 1 sha, but the size of sha list is {len(args.sha)}'
            new_patches = create_patch_for_sha_with_patch_file(
                args.patch_file, args.sha[0], start_version, patch_list)
        else:
            new_patches = create_patches_for_sha_list(args.sha, start_version, patch_list)
        patch_list.extend(new_patches)

    patch_list.sort()
    patch_list.save_to_file()
    if not patch_list.check_patches():
        return False

    if args.pr or args.sha:
        assert(args.reason), (
            'Reason `--reason` must be specified with a PR or with a SHA.'
        )
    else:
        # Only sort the patches and return.
        return True

    if not args.no_verify_merge:
        print('Verifying merge...')
        print('Verifying merge with git am ...')
        source_manager.setup_sources(git_am=True)
        print('Verifying merge with patch ...')
        source_manager.setup_sources()
    if not args.no_create_cl:
        cherry = True if args.sha else False
        create_cl(new_patches, args.reason, args.bug, cherry)
    return True


if __name__ == '__main__':
    sys.exit(0 if main() else 1)
