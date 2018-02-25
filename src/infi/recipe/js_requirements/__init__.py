__import__("pkg_resources").declare_namespace(__name__)

import tarfile
import json
import os
import re
import errno

from six import BytesIO
from six.moves import urllib
from infi.pyutils import lazy
from collections import defaultdict
from semantic_version import Version, Spec
import zc.buildout.easy_install
import codecs


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

        if buildout_section.get('js_versions', False):
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

    @staticmethod
    def _validate_hash(data, shasum):
        """
        Validates the data shasum vs the given shasum from the repository
        :param data: data to calculate shasum for it
        :param shasum: the shasum provided
        :return: True on valid, False otherwise
        """
        from hashlib import sha1
        digest = sha1(data).hexdigest()
        if digest == shasum:
            return True
        else:
            print('Invalid shasum, got: {}  , expected: {}'.format(digest, shasum))
            return False

    @lazy.cached_method
    def _get_metadata(self, pkg_name, ver=None):
        """
        Gets the JSON metadata object from the class specified REGISTRY
        :param pkg_name: str The package name to query about
        :param ver: semantic_version.Version if provided will only get metadata for specified version,
                    else will retrieve general metadata
        :return: Dict
        """
        if ver:
            url = urllib.parse.urljoin(self.REGISTRY, '/'.join([pkg_name, str(ver)]))
        else:
            url = urllib.parse.urljoin(self.REGISTRY, pkg_name)
        response = urllib.request.urlopen(url)
        pkg_metadata = json.load(self.reader(response))
        return pkg_metadata

    def _resolve_dependencies(self):
        """
        Resolves the dependencies according to the specified constraints. Starts with the dependencies provided
        from the buildout.cfg and continue resolving further package dependencies in a BFS manner.
        :return: Dict(package_name:str = selected_version:semantic_version.Version)
        """
        # Initialization of the BFS
        resolved = dict()
        bfs_stack = list()
        for requirement_name, spec_str in self.spec_requirements:
            self._add_spec(requirement_name, spec_str)
            bfs_stack.append(requirement_name)

        # Main loop
        while bfs_stack:
            # Stack Unwind
            requirement_name = bfs_stack.pop(0)
            available_versions = self._get_available_versions(requirement_name)
            spec = self._get_spec(requirement_name)
            matching_version = spec.select(available_versions)
            if matching_version is None:
                msg = 'Unmatched dependency for {}\nSpecification requirement: {}\nAvailable versions: {}'
                error = msg.format(requirement_name, spec, ', '.join(reversed(map(str, available_versions))))
                raise RequirementMatchError(error)

            resolved[requirement_name] = matching_version

            # Stack population
            dependencies = self._get_dependencies(requirement_name, matching_version)
            for dependency, dep_ver in dependencies.items():
                self._add_spec(dependency, dep_ver)
                bfs_stack.append(dependency)

        return resolved

    def _add_spec(self, requirement_name, spec_str):
        """
        Adds a version specification (constraint) to the set of constraints for each package requirement
        :param requirement_name: The package name as string
        :param spec_str: semantic_version.Version constraint as string (e.g. >=1.1.0, ~2.3.0, ^3.4.5-pre.2+build.4)
        """
        spec_str = spec_str or '>=0.0.0'
        self.versions_spec[requirement_name].add(spec_str)

    def _get_spec(self, requirement_name):
        """
        Creates a version range specification from the set of constraints for the required package name.
        :param requirement_name: The package name as string
        :return: semantic_version.Spec(all version specification)
        """
        return Spec(','.join(self.versions_spec[requirement_name]))

    def _download_package(self, pkg_metadata, validate=True):
        """
        Downloads specified package using the NPM REGISTRY
        :param pkg_metadata: Metadata object
        :param validate: If true performs shasum validation on the file downloaded. default=True
        :return: True on success, false otherwise
        """
        pkg_name = pkg_metadata.get('name')
        dist = pkg_metadata.get('dist')
        tar_url = dist.get('tarball')
        shasum = dist.get('shasum')

        print('\tDownloading {} from {}'.format(pkg_name, tar_url))

        tar_data = urllib.request.urlopen(tar_url)
        compressed_file = BytesIO(tar_data.read())
        if validate and not self._validate_hash(compressed_file.read(), shasum):
            return None

        compressed_file.seek(0)
        with tarfile.open(fileobj=compressed_file, mode='r:gz') as tar:
            package_folder = os.path.join(self.output_folder, pkg_name)
            tar.extractall(self.output_folder)
        if os.path.isdir(os.path.join(self.output_folder, 'package')):
            self.created(package_folder)
            os.rename(os.path.join(self.output_folder, 'package'), package_folder)
            if self.symlink_dir and 'main' in pkg_metadata:
                self._create_symlink(package_folder, pkg_metadata['main'])

    def _create_symlink(self, source_path, main):
        """
        Wrapper method for creating a correct symlink (or windows/ntfs link) to the main file
        :param source_path: str
        :param main: str
        """
        main_file = os.path.realpath(os.path.join(source_path, main))
        if not os.path.isfile(main_file):
            main_file += '.js'
        main_file_name = os.path.basename(main_file)
        with ChangeDirectory(os.path.realpath(self.symlink_dir)) as cd:
            file_path = os.path.join(cd.current, main_file_name)
            self.created(file_path)
            if os.path.islink(file_path):
                os.remove(file_path)
            symlink(main_file, main_file_name)

    @lazy.cached_method
    def _get_available_versions(self, requirement_name):
        """
        Retrieves a sorted list of all available versions for the require package
        :param requirement_name: str
        :return: List(semantic_version.Version)
        """
        return sorted(map(Version, self._get_metadata(requirement_name).get('versions', dict()).keys()))

    @lazy.cached_method
    def _get_dependencies(self, requirement_name, version):
        """
        Retrieves all of the package dependencies of a specific package and version and returns a dictionary of
        package dependency name and the spec str (e.g. >=3.1.1)
        :param requirement_name: str
        :param version: semantic_version.Version
        :return: Dict(pkg_name:str = spec:str)
        """
        return self._get_metadata(requirement_name, version).get('dependencies', dict())

    def _write_lock(self, selected_versions):
        versions = dict([(req, str(ver)) for req, ver in selected_versions.items()])
        self.created('.package-lock.json')
        with open('.package-lock.json', 'wb') as pljson:
            json.dump(versions, pljson)

    def _setup(self):
        """
        Main function to be run by buildout
        :return: List(all paths/files created:str)
        """
        mkdir_p(self.output_folder)
        if self.symlink_dir:
            mkdir_p(self.symlink_dir)
        selected_versions = self._resolve_dependencies()
        if selected_versions:
            self._write_lock(selected_versions)
            print('\n\nVersions Selected for downloading:\n')
            print('\t' + '\n\t'.join(['{}: {}'.format(req, ver) for req, ver in selected_versions.items()]))
            print('\n\nStarting Download:\n')
            for pkg_name, version in selected_versions.items():
                pkg_metadata = self._get_metadata(pkg_name, version)
                self._download_package(pkg_metadata)

        return self.created()

    def update(self):
        return self._setup()

    def install(self):
        return self._setup()


def get_bool(options, name, default=False):
    value = options.get(name)
    if not value:
        return default
    if value == 'true':
        return True
    elif value == 'false':
        return False
    else:
        raise zc.buildout.UserError(
            "Invalid value for %s option: %s" % (name, value))


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class ChangeDirectory(object):
    """
    ChangeDirectory is a context manager that allowing
    you to temporary change the working directory.

    >>> import tempfile
    >>> td = os.path.realpath(tempfile.mkdtemp())
    >>> currentdirectory = os.getcwd()
    >>> with ChangeDirectory(td) as cd:
    ...     assert cd.current == td
    ...     assert os.getcwd() == td
    ...     assert cd.previous == currentdirectory
    ...     assert os.path.normpath(os.path.join(cd.current, cd.relative)) == cd.previous
    ...
    >>> assert os.getcwd() == currentdirectory
    >>> with ChangeDirectory(td) as cd:
    ...     os.mkdir('foo')
    ...     with ChangeDirectory('foo') as cd2:
    ...         assert cd2.previous == cd.current
    ...         assert cd2.relative == '..'
    ...         assert os.getcwd() == os.path.join(td, 'foo')
    ...     assert os.getcwd() == td
    ...     assert cd.current == td
    ...     os.rmdir('foo')
    ...
    >>> os.rmdir(td)
    >>> with ChangeDirectory('.') as cd:
    ...     assert cd.current == currentdirectory
    ...     assert cd.current == cd.previous
    ...     assert cd.relative == '.'
    """

    def __init__(self, directory):
        self._dir = directory
        self._cwd = os.getcwd()
        self._pwd = self._cwd

    @property
    def current(self):
        return self._cwd

    @property
    def previous(self):
        return self._pwd

    @property
    def relative(self):
        c = self._cwd.split(os.path.sep)
        p = self._pwd.split(os.path.sep)
        ll = min(len(c), len(p))
        i = 0
        while i < ll and c[i] == p[i]:
            i += 1
        return os.path.normpath(os.path.join(*(['.'] + (['..'] * (len(c) - i)) + p[i:])))

    def __enter__(self):
        self._pwd = self._cwd
        os.chdir(self._dir)
        self._cwd = os.getcwd()
        return self

    def __exit__(self, *args):
        os.chdir(self._pwd)
        self._cwd = self._pwd


def symlink(source, link_name):
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
