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

"""
Package to manage LLVM sources when building a toolchain.
"""

import logging
from pathlib import Path
from typing import List, Optional
import os
import re
import shutil
import string
import subprocess
import sys

from llvm_android.toolchain_errors import ToolchainErrorCode, ToolchainError
from llvm_android import android_version, hosts, paths, utils


def logger():
    """Returns the module level logger."""
    return logging.getLogger(__name__)


def apply_patches(source_dir, svn_version, patch_json, patch_dir, git_am,
                  failure_mode):
    """Apply patches in $patch_dir/$patch_json to $source_dir.

    Invokes external/toolchain-utils/llvm_tools/patch_manager.py to apply the
    patches.
    """
    assert failure_mode, "Invalid failure_mode"
    patch_manager_cmd = [
        sys.executable,
        str(paths.TOOLCHAIN_UTILS_DIR / 'llvm_tools' / 'patch_manager.py'),
        '--svn_version', str(svn_version),
        '--patch_metadata_file', str(patch_json),
        '--src_path', str(source_dir),
        '--failure_mode', failure_mode
    ]

    patch_dir = os.getcwd()
    if git_am:
      patch_manager_cmd.append('--git_am')
      """Run git am in the source directory"""
      patch_dir=source_dir

    return utils.check_output(patch_manager_cmd, cwd=patch_dir)

class PatchInfo:
    """Holds info for a round of patch applications."""
    def __init__(self) -> None:
        self.applied_patches = []
        self.failed_patches = []
        self.inapplicable_patches = []

    @staticmethod
    def get_subject(patch_file: Path) -> str:
        contents = patch_file.read_text()
        # Parse patch generated by `git format-patch`.
        matches = re.search('Subject: (.*)\n', contents)
        if matches:
            subject = matches.group(1)
            trim_str = '[PATCH] '
            if subject.startswith(trim_str):
                subject = subject[len(trim_str):]
            return subject
        # Parse patch generated by `git show`. The format is used by patches synced from chromiumos.
        matches = re.search(r'^Date:.+\n\s*\n\s*(.+)', contents, re.MULTILINE)
        assert matches, f'failed to parse subject from {patch_file}'
        subject = matches.group(1)
        return subject

    @staticmethod
    def format_patch_line(patch_file: Path) -> str:
        url_prefix = 'https://android.googlesource.com/toolchain/llvm_android/+/' +\
            '{{scripts_sha}}'
        assert patch_file.is_file(), f"patch file doesn't exist: {patch_file}"
        patch_name = patch_file.name
        if re.match('([0-9a-f]+)(_v[0-9]+)?\.patch$', patch_name):
            url_suffix = '/patches/cherry/' + patch_name
            link_text = PatchInfo.get_subject(patch_file)
        else:
            url_suffix = '/patches/' + patch_name
            link_text = patch_name
        return f'- [{link_text}]({url_prefix}{url_suffix})'

def write_source_info(source_dir: str, pi: PatchInfo) -> None:
    output = []
    base_revision = android_version.get_git_sha()
    github_url = 'https://github.com/llvm/llvm-project/commits/' + base_revision
    output.append(f'Base revision: [{base_revision}]({github_url})')
    output.append('')

    applied_patches = []
    for patch in pi.applied_patches:
        applied_patches.append(pi.format_patch_line(Path(patch)))

    output.extend(sorted(applied_patches))

    with open(paths.OUT_DIR / 'clang_source_info.md', 'w') as outfile:
        outfile.write('\n'.join(output))


def get_source_info(source_dir: str, patch_output: str) -> PatchInfo:
    pi = PatchInfo()
    patches = patch_output.strip().splitlines()
    patches_iter = iter(patches)
    assert next(patches_iter) == 'The following patches applied successfully:'

    applied = True # applied/successful patches
    failed = False # failed patches
    inapplicable = False # inapplicable patches

    while True:
        patch = next(patches_iter, None)
        # We may optionally have an empty line followed by patches that were not
        # applicable.
        if patch == '':
            ni = next(patches_iter)
            if ni == 'The following patches were not applicable:':
                applied = False
                failed = False
                inapplicable = True
                continue
            if ni == 'The following patches failed to apply:':
                applied = False
                failed = True
                inapplicable = False
                continue
        elif patch is None:
            break
        assert patch.endswith('.patch')
        if applied:
            pi.applied_patches.append(patch)
        if failed:
            pi.failed_patches.append(patch)
        if inapplicable:
            pi.inapplicable_patches.append(patch)

    return pi

def setup_sources(git_am=False, llvm_rev=None, skip_apply_patches=False, continue_on_patch_errors=False) -> Optional[ToolchainError]:
    """Setup toolchain sources into paths.LLVM_PATH.

    Copy toolchain/llvm-project into paths.LLVM_PATH or clone from upstream.
    Apply patches per the specification in
    toolchain/llvm_android/patches/PATCHES.json.  The function overwrites
    paths.LLVM_PATH only if necessary to avoid recompiles during incremental builds.
    """
    # Return the error messages upon failure.
    ret: Optional[ToolchainError] = []
    source_dir = paths.LLVM_PATH
    tmp_source_dir = source_dir.parent / (source_dir.name + '.tmp')
    if os.path.exists(tmp_source_dir):
        shutil.rmtree(tmp_source_dir)

    # mkdir parent of tmp_source_dir if necessary.
    tmp_source_parent = os.path.dirname(tmp_source_dir)
    if not os.path.exists(tmp_source_parent):
        os.makedirs(tmp_source_parent)

    if not llvm_rev:
        # Copy llvm source tree to a temporary directory.
        copy_from = paths.TOOLCHAIN_LLVM_PATH
        logger().info(f'No llvm revision provided, copying from {copy_from}')
        # Use 'cp' instead of shutil.copytree.  The latter uses copystat and retains
        # timestamps from the source.  We instead use rsync below to only update
        # changed files into source_dir.  Using 'cp' will ensure all changed files
        # get a newer timestamp than files in $source_dir.
        # Note: Darwin builds don't copy symlinks with -r.  Use -R instead.
        reflink = '--reflink=auto' if hosts.build_host().is_linux else '-c'
        try:
          cmd = ['cp', '-Rf', reflink, copy_from, tmp_source_dir]
          subprocess.check_call(cmd)
        except subprocess.CalledProcessError:
          # Fallback to normal copy.
          cmd = ['cp', '-Rf', copy_from, tmp_source_dir]
          subprocess.check_call(cmd)

        if git_am:
          # To avoid clobbering the source tree's git objects, remove
          # out/llvm-project/.git and copy .repo/projects/toolchain/llvm-project.git there
          tmp_out_git_dir = tmp_source_dir / '.git'
          copy_from_git = os.path.abspath(os.path.join(tmp_source_dir, os.readlink(tmp_out_git_dir)))

          cmd = ['rm', '-rf', tmp_out_git_dir]
          subprocess.check_call(cmd)

          cmd = ['cp', '-Rf', '-L', copy_from_git, tmp_out_git_dir]
          subprocess.check_call(cmd)
    else:
        logger().info(f'Fetching {llvm_rev} from https://github.com/llvm/llvm-project.git')
        if not os.path.exists(tmp_source_dir):
            os.makedirs(tmp_source_dir)
        with utils.chdir_context(tmp_source_dir):
            cmd = ['git', 'init']
            subprocess.check_call(cmd)
            cmd = ['git', 'remote', 'add', 'origin', 'https://github.com/llvm/llvm-project.git']
            subprocess.check_call(cmd)
            cmd = ['git', 'fetch', '--depth=1', 'origin', llvm_rev]
            subprocess.check_call(cmd)
            cmd = ['git', 'reset', '--hard', 'FETCH_HEAD']
            subprocess.check_call(cmd)

    # patch source tree
    patch_dir = paths.SCRIPTS_DIR / 'patches'
    patch_json = os.path.join(patch_dir, 'PATCHES.json')
    svn_version = android_version.get_svn_revision_number()

    if not skip_apply_patches:
      failure_mode = 'continue' if continue_on_patch_errors else 'fail'
      patch_output = apply_patches(tmp_source_dir, svn_version, patch_json,
                                   patch_dir, git_am, failure_mode)
      logger().info(patch_output)
      pi = get_source_info(tmp_source_dir, patch_output)
      write_source_info(tmp_source_dir, pi)
      ret = ToolchainError(ToolchainErrorCode.PATCH_ERROR, str(pi.failed_patches))

    # Copy tmp_source_dir to source_dir if they are different.  This avoids
    # invalidating prior build outputs.
    if not os.path.exists(source_dir):
        os.rename(tmp_source_dir, source_dir)
    else:
        # Without a trailing '/' in $SRC, rsync copies $SRC to
        # $DST/BASENAME($SRC) instead of $DST.
        tmp_source_dir_str = str(tmp_source_dir) + '/'

        # rsync to update only changed files.  Use '-c' to use checksums to find
        # if files have changed instead of only modification time and size -
        # which could have inconsistencies.  Use '--delete' to ensure files not
        # in tmp_source_dir are deleted from $source_dir.
        subprocess.check_call(['rsync', '-r', '--delete', '--links', '-c',
                               tmp_source_dir_str, source_dir])

        shutil.rmtree(tmp_source_dir)
    remote, url = try_set_git_remote(source_dir)
    logger().info(f'git remote url: remote: {remote} url: {url}')
    return ret


def try_set_git_remote(source_dir):
    AOSP_URL = 'https://android.googlesource.com/toolchain/llvm-project'

    def get_git_remote_url(remote=None):
        try:
            if not remote:
                remote = utils.check_output([
                    'git', 'rev-parse', '--abbrev-ref', '--symbolic-full-name',
                    '@{upstream}'
                ]).strip()
                remote = remote.split('/')[0]
            url = utils.check_output(['git', 'remote', 'get-url',
                                      remote]).strip()
            return (remote, url)
        except:
            return (remote, None)

    git_dir = source_dir / '.git'
    if not git_dir.is_dir():
        return (None, None)

    with utils.chdir_context(git_dir):
        remote, url = get_git_remote_url()
        if url != AOSP_URL:
            if not remote:
                remote = 'origin'
            try:
                if Path('config').is_symlink():
                    link = utils.check_output(['readlink', 'config']).strip()
                    utils.check_call(['rm', '-f', 'config'])
                    utils.check_call(['cp', link, 'config'])
                if remote:
                    utils.check_call(
                        ['git', 'remote', 'set-url', remote, AOSP_URL])
                else:
                    utils.check_call(['git', 'remote', 'add', remote, AOSP_URL])
                remote, url = get_git_remote_url(remote)
            except:
                pass
    return (remote, url)
