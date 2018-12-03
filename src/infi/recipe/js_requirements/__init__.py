__import__("pkg_resources").declare_namespace(__name__)

import shutil
import tarfile
import json
import os
import re
import errno
import contextlib

from six import BytesIO
from six.moves import urllib
import requests
from collections import defaultdict
from semantic_version import Version, Spec
import codecs
import zc.buildout


class DependencyError(Exception):
    """
    Raised for dependency resolution errors
    """
    pass


class RequirementMatchError(Exception):
    """ Raised when we can't find a matching version for
    """
    pass


class JSDep(object):
    """
    Responsible for the resolving, downloading, extraction and validation
    of all javascript packages required by the project.
    """
    REGISTRY = "https://registry.npmjs.org/"
    MIRROR = "https://skimdb.npmjs.com/registry/"
    DEFAULT_DIRECTORY = "parts/js/"

    def __init__(self, buildout, name, options):
        super(JSDep, self).__init__()
        self.buildout = buildout
        self.name = name
        buildout_section = buildout['buildout']
        js_options = buildout['js-requirements']
        self.newest = get_bool(buildout_section, 'newest')
        if get_bool(buildout_section, 'js_versions'):
            js_versions_section = buildout['js_versions']
            self.spec_requirements = js_versions_section.items()
        else:
            spec_split = re.compile("^([^!<>=~^]+)([!<>=~^]+[^!<>=~^]+)?$")
            self.spec_requirements = [spec_split.findall(pkg)[0] for pkg in eval(js_options['javascript-packages'])]
        self.symlink_dir = options['symlink-to-directory'] = js_options['symlink-to-directory']

        self.created = options.created
        self.output_folder = js_options['js-directory'] or self.DEFAULT_DIRECTORY
        self.versions_spec = defaultdict(set)
        self.reader = codecs.getreader('utf-8')
        self.metadatas = {}

    @staticmethod
    def _validate_hash(data, shasum):
        """
        Validates the data shasum against the given shasum from the repository
        :param bytes|str data: Data to evaluate
        :param str shasum: Provided shasum hash
        :return bool: True on valid, False otherwise
        """
        from hashlib import sha1
        digest = sha1(data).hexdigest()
        if digest == shasum:
            return True
        else:
            print('Invalid shasum, got: {}  , expected: {}'.format(digest, shasum))
            return False

    def _get_metadata(self, pkg_name):
        """
        Gets the JSON metadata object from the class specified REGISTRY
        :param str pkg_name: The package name to query about
        :return dict: Package metadata dictionary
        """
        pkg_name = urllib.parse.quote(pkg_name, safe='@')
        if self.metadatas.get(pkg_name):
            return self.metadatas.get(pkg_name)
        else:
            url = urllib.parse.urljoin(self.REGISTRY, pkg_name)
            try:
                pkg_metadata = requests.get(url).json()
                self.metadatas[pkg_name] = pkg_metadata
                return pkg_metadata
            except urllib.error.HTTPError as e:
                print('Could not download {} from: {} with error: {}'. format(pkg_name, url, e.msg))
                exit(-1)

    def _resolve_dependencies(self):
        """
        Resolves the dependencies according to the specified constraints. Starts with the dependencies provided
        from the buildout.cfg and continue resolving further package dependencies in a BFS manner.
        :return dict: {package_name:str = Version}
        """
        matching_versions = dict()

        # Initialization of the BFS
        bfs_stack = list()
        for requirement_name, spec_str in sorted(self.spec_requirements, key=lambda x: x[0].lower()):
            self._add_spec(requirement_name, spec_str)
            bfs_stack.append(requirement_name)

        # Main loop
        while bfs_stack:
            # Stack Unwind
            requirement_name = bfs_stack.pop(0)
            available_versions = self._get_available_versions(requirement_name)
            spec = self._get_spec(requirement_name)
            best_matching_version = spec.select(available_versions)
            if best_matching_version is None:
                msg = 'Unmatched dependency for {}\nSpecification requirement: {}\nAvailable versions: {}\n' \
                      'Use NPM semver calculator to resolve: https://semver.npmjs.com/'
                error = msg.format(requirement_name, spec, ', '.join(reversed(map(str, available_versions))))
                raise RequirementMatchError(error)

            matching_versions[requirement_name] = best_matching_version

            # BFS stack population with dependencies
            dependencies = self._get_dependencies(requirement_name, best_matching_version)
            for dependency_name, dependency_version in dependencies:
                self._add_spec(dependency_name, dependency_version)
                bfs_stack.append(dependency_name)

        return matching_versions

    def _add_spec(self, requirement_name, spec_str):
        """
        Adds a version specification (constraint) to the set of constraints for each package requirement
        :param str requirement_name: The package name
        :param str spec_str: Semantic version constraint as string (e.g. >=1.1.0, ~2.3.0, ^3.4.5-pre.2+build.4)
        """
        spec_str = spec_str or '>=0.0.0'
        spec_str = spec_str.replace(' ', '')
        spec_str = '~' + spec_str.replace('.x', '.0') if '.x' in spec_str else spec_str
        self.versions_spec[requirement_name].add(spec_str)

    def _get_spec(self, requirement_name):
        """
        Creates a version range specification from the set of constraints for the required package name.
        :param str requirement_name: The package name
        :return Spec: All version specification
        """
        return Spec(','.join(self.versions_spec[requirement_name]))

    def _download_package(self, pkg_metadata, validate=True):
        """
        Downloads specified package using the NPM REGISTRY
        :param dict pkg_metadata: Metadata object
        :param bool validate: If true performs shasum validation on the file downloaded (default True)
        :return bool: True on success, false otherwise
        """
        pkg_name = pkg_metadata.get('name')
        package_folder = os.path.join(self.output_folder, pkg_name)
        if os.path.isdir(package_folder):
            if self.newest:
                shutil.rmtree(package_folder)
            else:
                if self.symlink_dir and 'main' in pkg_metadata:
                    self._create_symlink(package_folder, pkg_metadata['main'])
                print('\t{} already installed, use --newest.'.format(pkg_name))
                return
        dist = pkg_metadata.get('dist')
        tar_url = dist.get('tarball')
        shasum = dist.get('shasum')

        print('\tDownloading {} from {}'.format(pkg_name, tar_url))

        tar_data = requests.get(tar_url)
        compressed_file = BytesIO(tar_data.content)
        if validate and not self._validate_hash(compressed_file.read(), shasum):
            return None

        compressed_file.seek(0)
        with tarfile.open(fileobj=compressed_file, mode='r:gz') as tar:
            tar.extractall(self.output_folder)
        if os.path.isdir(os.path.join(self.output_folder, 'package')):
            # self.created(package_folder)
            shutil.move(os.path.join(self.output_folder, 'package'), package_folder)
            if self.symlink_dir and 'main' in pkg_metadata:
                self._create_symlink(package_folder, pkg_metadata['main'])

    def _create_symlink(self, source_path, main):
        """
        Wrapper method for creating a correct symlink (or windows/ntfs link) to the main file
        :param str source_path: Path to all downloaded packages
        :param str main: The main file name/location in the package
        """
        main_file = os.path.realpath(os.path.join(source_path, main))
        if not os.path.isfile(main_file):
            main_file += '.js'
        main_file_name = os.path.basename(main_file)
        with change_working_directory(os.path.realpath(self.symlink_dir)) as cd:
            file_path = os.path.join(cd, main_file_name)
            self.created(file_path)
            if os.path.islink(file_path):
                os.remove(file_path)
            symlink(main_file, main_file_name)

    def _get_available_versions(self, requirement_name):
        """
        Retrieves a sorted list of all available versions for the require package
        :param str requirement_name: Package name to query
        :return list: [Version]
        """
        return sorted(map(Version, self._get_metadata(requirement_name).get('versions', dict()).keys()))

    def _get_dependencies(self, requirement_name, version):
        """
        Retrieves all of the package dependencies of a specific package and version and returns a dictionary of
        package dependency name and the spec str (e.g. >=3.1.1)
        :param str requirement_name: Package name to query
        :param Version version: Specified version to query
        :return dict: {pkg_name:str = spec:str}
        """
        pkg_metadata = self._get_metadata(requirement_name)
        versions = pkg_metadata.get('versions', dict())
        version = versions.get(str(version), dict())
        return sorted(version.get('dependencies', dict()).items())

    def _write_lock(self, selected_versions):
        versions = dict([(req, str(ver)) for req, ver in selected_versions.items()])
        lock_path = os.path.join(self.output_folder, '.package-lock.json')
        self.created(lock_path)
        with open(lock_path, 'w') as pljson:
            json.dump(versions, pljson)

    def _setup(self):
        """
        Main function to be run by buildout
        :return: List(all paths/files created:str)
        """
        mkdir_p(self.output_folder)
        if self.symlink_dir:
            mkdir_p(self.symlink_dir)
        try:
            selected_versions = self._resolve_dependencies()
            if selected_versions:
                self._write_lock(selected_versions)
                print('\n\nVersions Selected for downloading:\n')
                print('\t' + '\n\t'.join(['{}: {}'.format(req, ver) for req, ver in selected_versions.items()]) + '\n')
                for pkg_name, version in selected_versions.items():
                    pkg_metadata = self._get_metadata(pkg_name)
                    version_metadata = pkg_metadata.get('versions', dict()).get(str(version), dict())
                    self._download_package(version_metadata)
        except (RequirementMatchError, DependencyError) as e:
            print(e.message)
        return self.created()

    update = _setup
    install = _setup


def get_bool(options, name, default=False):
    """
    Evaluates a dictionary string to boolean
    :param dict options:
    :param str name: dict key
    :param bool default: Default evaluation (default False)
    :return bool:
    """
    value = options.get(name)
    if not value:
        return default
    if value.lower() == 'true':
        return True
    elif value.lower() == 'false':
        return False
    else:
        raise zc.buildout.UserError(
            "Invalid value for %s option: %s" % (name, value))


def mkdir_p(path):
    """
    Safe creation of nested path with parents
    :param str path: Path to create
    """
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


@contextlib.contextmanager
def change_working_directory(path):
    """
    A context manager to change current working directory to path, change back on exit
    """
    prev_cwd = os.getcwd()
    os.chdir(path)
    try:
        yield os.getcwd()
    finally:
        os.chdir(prev_cwd)


def symlink(source, link_name):
    """
    Creates a symbolic link on *NIX and Windows
    :param str source: Source path
    :param str link_name: Name of the newly created link
    """
    os_symlink = getattr(os, "symlink", None)
    if callable(os_symlink):
        os_symlink(source, link_name)
    else:
        import ctypes
        csl = ctypes.windll.kernel32.CreateSymbolicLinkW
        csl.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        csl.restype = ctypes.c_ubyte
        flags = 1 if os.path.isdir(source) else 0
        if csl(link_name, source, flags) == 0:
            raise ctypes.WinError()
