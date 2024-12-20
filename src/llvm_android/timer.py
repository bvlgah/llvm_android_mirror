#
# Copyright (C) 2021 The Android Open Source Project
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
from time import time
from datetime import timedelta

import atexit
import os
from llvm_android.build_info import ToolchainBuild

class Timer:
    times = {}
    def __init__(self, descr):
        self.descr = descr

    def __enter__(self):
        self.start = time()

    def __exit__(self, t, value, traceback):
        end = time()
        build_time = end - self.start
        type(self).times[self.descr] = build_time
        ToolchainBuild.BuildTime.append({"step": self.descr, "time": round(build_time, 2)})

    @classmethod
    def report(cls):
        """Return list of '<duration> <description>' entries."""
        num_cores = os.cpu_count()
        report = f'Cores: {num_cores}\n\n'

        pretty_print = lambda t: str(timedelta(seconds=int(t)))
        result = sorted(cls.times.items(), key=lambda item: item[1], reverse=True)
        report = report + '\n'.join(f'{pretty_print(t)} {d}' for d, t in result)

        return report

    @classmethod
    def report_to_file(cls, outfile):
        with open(outfile, 'w') as out:
            out.write(cls.report())

    @classmethod
    def register_atexit(cls, outfile):
        """Register report_to_file(outfile) to run at exit."""
        atexit.register(cls.report_to_file, outfile)
