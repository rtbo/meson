# Copyright 2013-2021 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from gettext import find

from pip import main
from .base import ExternalDependency, DependencyException, DependencyTypeName
from .pkgconfig import PkgConfigDependency
from ..mesonlib import (Popen_safe, OptionKey)
from ..programs import ExternalProgram
from ..compilers import DCompiler
from ..compilers.d import find_ldc_dmd_frontend_version
from .. import mlog
import re
import os
import copy
import json
import platform
import typing as T

if T.TYPE_CHECKING:
    from ..environment import Environment

class DubDependency(ExternalDependency):
    class_dubbin = None

    def __init__(self, name: str, environment: 'Environment', kwargs: T.Dict[str, T.Any]):
        super().__init__(DependencyTypeName('dub'), environment, kwargs, language='d')
        self.name = name
        self.module_path: T.Optional[str] = None

        _temp_comp = super().get_compiler()
        assert isinstance(_temp_comp, DCompiler)
        self.compiler = _temp_comp

        if 'required' in kwargs:
            self.required = kwargs.get('required')

        if DubDependency.class_dubbin is None:
            self.dubbin = self._check_dub()
            DubDependency.class_dubbin = self.dubbin
        else:
            self.dubbin = DubDependency.class_dubbin

        if not self.dubbin:
            if self.required:
                raise DependencyException('DUB not found.')
            self.is_found = False
            return

        assert isinstance(self.dubbin, ExternalProgram)
        mlog.debug('Determining dependency {!r} with DUB executable '
                   '{!r}'.format(name, self.dubbin.get_path()))

        # if an explicit version spec was stated, use this when querying Dub
        main_pack_spec = name
        if 'version' in kwargs:
            main_pack_spec = f'{name}@{kwargs["version"]}'

        # we need to know the target architecture
        arch = self.compiler.arch

        # we need to know the build type as well
        buildtype = 'debug'
        if OptionKey('buildtype') in environment.options:
            buildtype = environment.options[OptionKey('buildtype')]
        elif OptionKey('optimize') in environment.options:
            buildtype = 'release'

        def dub_fetch_package(pack_spec):
            mlog.log(mlog.bold(name), 'is not present locally. Attempting to fetch on Dub registry.')
            fetch_cmd = ['fetch', pack_spec]
            ret, _, fetch_err = self._call_dubbin(fetch_cmd)
            if ret != 0:
                mlog.debug('DUB fetch failed: ' + fetch_err)
            return ret == 0

        def dub_build_package(pack_id: str, conf: str):
            cmd = [
                'build', pack_id, '--config='+conf, '--arch='+arch, '--build='+buildtype,
                '--compiler='+self.compiler.get_exelist()[-1]
            ]
            mlog.log('Building DUB package', mlog.bold(pack_id))
            mlog.debug('Running DUB with ', cmd)
            ret, res, err = self._call_dubbin(cmd)
            if ret != 0:
                mlog.debug('DUB build failed: ', err)
            return ret == 0

        # Ask dub for the package
        describe_cmd = [
            'describe', main_pack_spec, '--arch=' + arch,
            '--build=' + buildtype, '--compiler=' + self.compiler.get_exelist()[-1]
        ]
        mlog.debug('Running ', describe_cmd)
        ret, res, err = self._call_dubbin(describe_cmd)

        # If not present, fetch and repeat
        if ret != 0 and 'locally' in err:
            if dub_fetch_package(main_pack_spec):
                ret, res, err = self._call_dubbin(describe_cmd)

        if ret != 0:
            mlog.debug('DUB describe failed: ' + err)
            self.is_found = False
            return

        comp_id = self.compiler.get_id().replace('llvm', 'ldc').replace('gcc', 'gdc')
        packages = {}
        description = json.loads(res)
        for package in description['packages']:
            packages[package['name']] = package

            # for each package:
            #  - fetch if not present
            #  - build for the right compiler if needed

            pack_id = f'{package["name"]}@{package["version"]}'

            if not os.path.exists(package['path']):
                dub_fetch_package(pack_id)

            if package['active'] and package['targetType'] in ['library', 'staticLibrary', 'dynamicLibrary']:
                dub_target = self._find_dub_build_target(description, package, comp_id)
                if dub_target is None:
                    mlog.debug(f'{pack_id} is not found for {comp_id}')
                    if not dub_build_package(pack_id, package['configuration']):
                        mlog.error('Could not build', mlog.bold(pack_id))
                        self.is_found = False
                        return

            ## check that the dependency is indeed a library, and that it is built for the right compiler
            if package['name'] == name:
                self.is_found = True

                not_lib = True
                if 'targetType' in package:
                    # sourceLibrary targets only consists of importFiles
                    # so no artefact is generated (#681)
                    if package['targetType'] == 'sourceLibrary':
                        continue
                    if package['targetType'] in ['library', 'sourceLibrary', 'staticLibrary', 'dynamicLibrary']:
                        not_lib = False

                if not_lib:
                    mlog.error(mlog.bold(name), "found but it isn't a library")
                    self.is_found = False
                    return

                dub_target = self._find_dub_build_target(description, package, comp_id)
                if dub_target is not None:
                    self.module_path = os.path.dirname(dub_target)
                else:
                    mlog.error('Could not find target of', mlog.bold(main_pack_spec))
                    self.is_found = False
                    return

                self.version = package['version']
                self.pkg = package

        if self.pkg['targetFileName'].endswith('.a'):
            self.static = True

        self.compile_args = []
        self.link_args = self.raw_link_args = []

        def add_buildsettings(bs):
            mlog.debug('Found build settings for '+main_pack_spec)
            for flag in bs['dflags']:
                self.compile_args.append(flag)
            for path in bs['importPaths']:
                self.compile_args.append('-I=' + path)
            for path in bs['stringImportPaths']:
                self.compile_args.append('-J=' + path)
            for ver in bs['versions']:
                self.compile_args.append('-version=' + ver)
            # for ver in bs['debugVersions']:
            #     self.compile_args.append('-debug=' + ver)

            for flag in bs['lflags']:
                self.link_args.append(flag)
            for file in bs['linkerFiles']:
                self.link_args.append(file)
            for lib in libs:
                self.link_args.append('-l' + lib)
            for file in bs['sourceFiles']:
                # it is possible to add libs checked out in repo with sourceFiles
                # we should as well link to them
                if file.endswith('.lib'):
                    self.link_args.append(file)

        # Handle dependencies
        libs = []

        def add_lib_args(field_name: str, target: T.Dict[str, T.Dict[str, str]]) -> None:
            if field_name in target['buildSettings']:
                for lib in target['buildSettings'][field_name]:
                    if lib not in libs:
                        libs.append(lib)
                        if os.name != 'nt':
                            pkgdep = PkgConfigDependency(lib, environment, {'required': 'true', 'silent': 'true'})
                            for arg in pkgdep.get_compile_args():
                                self.compile_args.append(arg)
                            for arg in pkgdep.get_link_args():
                                self.link_args.append(arg)
                            for arg in pkgdep.get_link_args(raw=True):
                                self.raw_link_args.append(arg)

        found_buildsettings = False

        for target in description['targets']:
            # add build settings for the dependency from the main target
            if target['rootPackage'] == name:
                add_buildsettings(target['buildSettings'])
                found_buildsettings = True

        if not found_buildsettings:
            mlog.error('Could not find build settings for', mlog.bold(name))
            self.is_found = False


    # This function finds the target of the provided JSON package, built for the right
    # compiler, architecture, configuration...
    # A value is returned only if the file exists
    def _find_dub_build_target(self, jdesc: T.Dict[str, str], jpack: T.Dict[str, str], comp_id: str):
        dub_build_path = os.path.join(jpack['path'], '.dub', 'build')

        if not os.path.exists(dub_build_path):
            print(dub_build_path, ' doesnot exist')
            return None

        # try to find a dir like library-debug-linux.posix-x86_64-ldc_2081-EF934983A3319F8F8FF2F0E107A363BA

        # fields are:
        #  - configuration
        #  - build type
        #  - platform
        #  - architecture
        #  - compiler id (dmd, ldc, gdc)
        #  - frontend id (2081 for 2.081.X)

        conf = jdesc['configuration']
        build_type = jdesc['buildType']
        platform = '.'.join(jdesc['platform'])
        arch = '.'.join(jdesc['architecture'])

        # Get D frontend version implemented in the compiler
        # gdc doesn't support this
        frontend_id = None
        frontend_version = None
        if comp_id in ['dmd', 'ldc']:
            ret, res = self._call_compbin(['--version'])[0:2]
            if ret != 0:
                mlog.error('Failed to run {!r}', mlog.bold(comp_id))
                return None
            d_ver_reg = re.search('v[0-9].[0-9][0-9][0-9].[0-9]', res) # Ex.: v2.081.2
            if d_ver_reg is not None:
                frontend_version = d_ver_reg.group()
                frontend_id = frontend_version.rsplit('.', 1)[0].replace('v', '').replace('.', '') # Fix structure. Ex.: 2081

        build_id = f'{conf}-{build_type}-{platform}-{arch}-{comp_id}'

        for entry in os.listdir(dub_build_path):
            if build_id in entry:
                if not build_id in entry:
                    continue
                if frontend_id and not frontend_id in entry and not frontend_version in entry:
                    continue

                build_dir = os.path.join(dub_build_path, entry)
                target = os.path.join(build_dir, jpack['targetFileName'])
                if os.path.exists(target):
                    return target
                else:
                    mlog.error('not exists', mlog.bold(target))

        return None


    def _call_dubbin(self, args: T.List[str], env: T.Optional[T.Dict[str, str]] = None) -> T.Tuple[int, str]:
        assert isinstance(self.dubbin, ExternalProgram)
        p, out, err = Popen_safe(self.dubbin.get_command() + args, env=env)
        return p.returncode, out.strip(), err.strip()

    def _call_compbin(self, args: T.List[str], env: T.Optional[T.Dict[str, str]] = None) -> T.Tuple[int, str]:
        p, out, err = Popen_safe(self.compiler.get_exelist() + args, env=env)
        return p.returncode, out.strip(), err.strip()

    def _check_dub(self) -> T.Union[bool, ExternalProgram]:
        dubbin: T.Union[bool, ExternalProgram] = ExternalProgram('dub', silent=True)
        assert isinstance(dubbin, ExternalProgram)
        if dubbin.found():
            try:
                p, out = Popen_safe(dubbin.get_command() + ['--version'])[0:2]
                if p.returncode != 0:
                    mlog.warning('Found dub {!r} but couldn\'t run it'
                                 ''.format(' '.join(dubbin.get_command())))
                    # Set to False instead of None to signify that we've already
                    # searched for it and not found it
                    dubbin = False
            except (FileNotFoundError, PermissionError):
                dubbin = False
        else:
            dubbin = False
        if isinstance(dubbin, ExternalProgram):
            mlog.log('Found DUB:', mlog.bold(dubbin.get_path()),
                     '(%s)' % out.strip())
        else:
            mlog.log('Found DUB:', mlog.red('NO'))
        return dubbin
