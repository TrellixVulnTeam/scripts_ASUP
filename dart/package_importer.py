#!/usr/bin/env python
# Copyright 2016 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import os
import paths
import shutil
import subprocess
import sys
import tempfile

sys.path += [os.path.join(paths.FUCHSIA_ROOT, 'third_party', 'pyyaml', 'lib')]
import yaml


LICENSE_FILES = ['LICENSE', 'LICENSE.txt']


IGNORED_EXTENSIONS = ['css', 'html', 'jpg', 'js', 'log', 'old', 'out',
                      'packages', 'png', 'snapshot', 'zip']

LOCAL_PACKAGES = {
  'analyzer': '//third_party/dart/pkg/analyzer',
  'analysis_server': '//third_party/dart/pkg/analysis_server',
  'flutter': '//third_party/dart-pkg/git/flutter/packages/flutter',
  'flutter_test': '//third_party/dart-pkg/git/flutter/packages/flutter_test',
  'front_end': '//third_party/dart/pkg/front_end',
  'func': '//third_party/dart/third_party/pkg/func',
  'intl': '//third_party/dart/third_party/pkg/intl',
  'kernel': '//third_party/dart/pkg/kernel',
  'linter': '//third_party/dart/third_party/pkg/linter',
  'telemetry': '//third_party/dart/pkg/telemetry',
  'typed_mock': '//third_party/dart/pkg/typed_mock',
}

FORBIDDEN_PACKAGES = ['mojo', 'mojo_services']

def parse_packages_file(dot_packages_path):
    """ parse the list of packages and paths in .packages file """
    packages = []
    with open(dot_packages_path) as dot_packages:
        # The packages specification says both '\r' and '\n' are valid line
        # delimiters, which matches Python's 'universal newline' concept.
        # Packages specification: https://github.com/dart-lang/dart_enhancement_proposals/blob/master/Accepted/0005%20-%20Package%20Specification/DEP-pkgspec.md
        contents = dot_packages.read()
        for line in unicode.splitlines(unicode(contents)):
            if line.startswith('#'):
                continue
            delim = line.find(':')
            if delim == -1:
                continue
            name = line[:delim]
            path = line[delim + 1:-1]
            packages.append((name, path))
    return packages


def parse_full_dependencies(yaml_path):
    """ parse the content of a pubspec.yaml """
    with open(yaml_path) as yaml_file:
        parsed = yaml.safe_load(yaml_file)
        if not parsed:
            raise Exception('Could not parse yaml file: %s' % yaml_file)
        package_name = parsed['name']
        get_deps = lambda dep_type: parsed[dep_type] if dep_type in parsed and parsed[dep_type] else {}
        deps = get_deps('dependencies')
        dev_deps = get_deps('dev_dependencies')
        dep_overrides = get_deps('dependency_overrides')
        return (package_name, deps, dev_deps, dep_overrides)


def parse_dependencies(yaml_path):
    """ parse the dependency map out of a pubspec.yaml """
    _, deps, _, _ = parse_full_dependencies(yaml_path)
    return deps


def write_build_file(build_gn_path, package_name, name_with_version, deps):
    """ writes BUILD.gn file for Dart package with dependencies """
    with open(build_gn_path, 'w') as build_gn:
        build_gn.write('''# This file is generated by importer.py for %s

import("//build/dart/dart_library.gni")

dart_library("%s") {
  package_name = "%s"

  # This parameter is left empty as we don't care about analysis or exporting
  # these sources outside of the tree.
  sources = []

  disable_analysis = true

  deps = [
''' % (name_with_version, package_name, package_name))
        for dep in deps:
            if dep in LOCAL_PACKAGES:
                build_gn.write('    "%s",\n' % LOCAL_PACKAGES[dep])
            else:
                build_gn.write('    "//third_party/dart-pkg/pub/%s",\n' % dep)
        build_gn.write('''  ]
}
''')


def read_package_versions(base):
    '''Scans the packages in a given directory.'''
    result = {}
    for (root, dirs, files) in os.walk(base):
        for dir in dirs:
            spec = os.path.join(root, dir, 'pubspec.yaml')
            if not os.path.exists(spec):
                continue
            with open(spec, 'r') as spec_file:
                data = yaml.safe_load(spec_file)
                result[data['name']] = data['version']
        break
    return result


def generate_package_diff(old_packages, new_packages, changelog):
    '''Writes a changelog file with package version changes.'''
    old = set(old_packages.iteritems())
    new = set(new_packages.iteritems())
    changed_keys = set([k for (k, _) in (old | new) - (old & new)])
    if not changed_keys:
        return
    max_key_width = max(map(lambda k: len(k), changed_keys))
    with open(changelog, 'w') as changelog_file:
        for key in sorted(changed_keys):
            old = old_packages.get(key, '<none>')
            new = new_packages.get(key, '<none>')
            changelog_file.write('%s %s --> %s\n' % (key.rjust(max_key_width),
                                                     old.rjust(10),
                                                     new.ljust(10)))


def main():
    parser = argparse.ArgumentParser('Import dart packages from pub')
    parser.add_argument('--pub', required=True,
                        help='Path to the pub executable')
    parser.add_argument('--pubspecs', nargs='+',
                        help='Paths to packages containing pubspec.yaml files')
    parser.add_argument('--projects', nargs='+',
                        help='Paths to projects containing dependency files')
    parser.add_argument('--output', required=True,
                        help='Path to the output directory')
    parser.add_argument('--changelog',
                        help='Path to the changelog file to write',
                        default=None)
    args = parser.parse_args()
    tempdir = tempfile.mkdtemp()
    try:
        importer_dir = os.path.join(tempdir, 'importer')
        os.mkdir(importer_dir)

        # Read the requested dependencies from the canonical packages.
        packages = {}
        additional_deps = {}
        for path in args.pubspecs:
            yaml_file = os.path.join(path, 'pubspec.yaml')
            package_name, _, dev_deps, _ = parse_full_dependencies(yaml_file)
            packages[package_name] = path
            additional_deps.update(dev_deps)

        # Generate a manifest containing all the dependencies we care about.
        manifest = {
            'name': 'importer',
        }
        dependencies = {}
        for package_name in packages.keys():
            dependencies[package_name] = 'any'
        for dep, version in additional_deps.iteritems():
            if dep in packages:
                continue
            dependencies[dep] = version
        for project in args.projects:
            yaml_file = os.path.join(project, 'dart_dependencies.yaml')
            project_deps = parse_dependencies(yaml_file)
            for dep, version in project_deps.iteritems():
                dependencies[dep] = version
        manifest['dependencies'] = dependencies
        overrides = {}
        for package_name, path in packages.iteritems():
            overrides[package_name] = {
                'path': path,
            }
        manifest['dependency_overrides'] = overrides
        with open(os.path.join(importer_dir, 'pubspec.yaml'), 'w') as pubspec:
            yaml.safe_dump(manifest, pubspec)

        old_packages = read_package_versions(args.output)

        # Use pub to load the dependencies into a local cache.
        pub_cache_dir = os.path.join(tempdir, 'pub_cache')
        os.mkdir(pub_cache_dir)
        env = os.environ
        env['PUB_CACHE'] = pub_cache_dir
        subprocess.check_call([args.pub, 'get'], cwd=importer_dir, env=env)

        # Walk the cache and copy the packages we are interested in.
        if os.path.exists(args.output):
            for (root, dirs, files) in os.walk(args.output):
                for dir in dirs:
                    if dir != '.git':
                        shutil.rmtree(os.path.join(root, dir))
                # Only process the root of the output tree.
                break

        pub_packages = parse_packages_file(os.path.join(importer_dir, '.packages'))
        for package in pub_packages:
            if package[0] in packages:
                # Skip canonical packages.
                continue
            if not package[1].startswith('file://'):
                continue
            source_dir = package[1][len('file://'):]
            if not os.path.exists(source_dir):
                continue
            if source_dir.find('pub.dartlang.org') == -1:
                print 'Package %s not from dartlang (%s), ignoring' % (package[0], source_dir)
                continue
            package_name = package[0]
            # Don't import packages that live canonically in the tree.
            if package_name in LOCAL_PACKAGES:
                continue
            if package_name in FORBIDDEN_PACKAGES:
                print 'Warning: dependency on forbidden package %s' % package_name
                continue
            # We expect the .packages file to point to a directory called 'lib'
            # inside the overall package, which will contain the LICENSE file
            # and other potentially useful directories like 'bin'.
            source_base_dir = os.path.dirname(os.path.abspath(source_dir))
            name_with_version = os.path.basename(source_base_dir)
            has_license = any(os.path.exists(os.path.join(source_base_dir, file_name))
                              for file_name in LICENSE_FILES)
            if not has_license:
                print 'Could not find license file for %s, skipping' % package_name
                continue
            pubspec_path = os.path.join(source_base_dir, 'pubspec.yaml')
            deps = []
            if os.path.exists(pubspec_path):
                deps = parse_dependencies(pubspec_path)
            dest_dir = os.path.join(args.output, package_name)
            shutil.copytree(source_base_dir, dest_dir,
                            ignore=shutil.ignore_patterns(
                                *('*.' + extension for extension in IGNORED_EXTENSIONS)))
            # We don't need the 'test' directory of packages we import as that
            # directory exists to test that package and some of our packages
            # have very heavy test directories, so nuke those.
            test_path = os.path.join(dest_dir, 'test')
            if os.path.exists(test_path):
                shutil.rmtree(test_path)
            write_build_file(os.path.join(dest_dir, 'BUILD.gn'), package_name,
                             name_with_version, deps)

        if args.changelog:
            new_packages = read_package_versions(args.output)
            generate_package_diff(old_packages, new_packages, args.changelog)

    finally:
        shutil.rmtree(tempdir)

if __name__ == '__main__':
    sys.exit(main())
