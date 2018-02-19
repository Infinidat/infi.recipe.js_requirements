__import__("pkg_resources").declare_namespace(__name__)

import tarfile
import json
import os
import re
import errno

from six import StringIO
from six.moves import urllib

import semver as semver
import zc.buildout.easy_install


class JSDep(object):
    REGISTRY = "https://registry.npmjs.org/"
    MIRROR = "https://skimdb.npmjs.com/registry/"
    DEFAULT_DIRECTORY = "parts/js/"

    def __init__(self, buildout, name, options):
        super(JSDep, self).__init__()
        self.buildout = buildout
        self.name = name
        self.options = options
        b_options = buildout['buildout']
        p_options = buildout['project']
        js_options = buildout['js-requirements']
        ver_split = re.compile("^([^<>=]+)([<>=]+)?([^<>=]+)?$")
        self.js_requirements = [ver_split.findall(pkg)[0] for pkg in eval(js_options['javascript-packages'])]
        options['js-directory'] = js_options['js-directory']
        self.develop_requirements = js_options['develop-js-requirements']
        options['develop-js-requirements'] = js_options['develop-js-requirements']
        options['develop-js-directory'] = js_options['develop-js-directory']
        self.created = options.created
        self.output_folder = options['js-directory'] or self.DEFAULT_DIRECTORY

    @staticmethod
    def _validate_hash(data, shasum):
        """
        Validates the data shasum vs the given shasum from the repository
        :param data: data to calculate shasum for it
        :param shasum: the shasum provided
        :return: True on valid, False otherwise
        """
        from hashlib import sha1
        return sha1(data).hexdigest() == shasum

    def _get_metadata(self, pkg_name, ver=None):
        """
        Gets the JSON metadata object from the class specified REGISTRY
        :param pkg_name: The package name to query about
        :param ver: if provided will only get metadata for specified version, else will retrieve metadata for all versions
        :return: Object with all the metadata values
        """
        if ver:
            url = urllib.parse.urljoin(self.REGISTRY, '/'.join([pkg_name, ver]))
        else:
            url = urllib.parse.urljoin(self.REGISTRY, pkg_name)
        request = urllib.request.urlopen(url)
        pkg_metadata = json.loads(request.read())
        return pkg_metadata

    def _download_package(self, pkg_metadata, version_info, validate=True):
        """
        Downloads package with version from the class specified REGISTRY
        :param pkg_metadata: The package name to download
        :param version_info: the version in semver dict format
        :param validate: If true performs shasum validation on the file downloaded, default=True
        :return: True on success, false otherwise
        """
        pkg_name = pkg_metadata.get('name')
        dist = pkg_metadata.get('dist')
        tar_url = dist.get('tarball')
        shasum = dist.get('shasum')
        tar_data = urllib.request.urlopen(tar_url)
        compressed_file = StringIO(tar_data.read())
        if validate and not self._validate_hash(compressed_file.read(), shasum):
            return None
        compressed_file.seek(0)
        tar = tarfile.open(fileobj=compressed_file, mode='r:gz')
        package_folder = os.path.join(self.output_folder, pkg_name)
        tar.extractall(self.output_folder)
        self.created(package_folder)
        os.rename(os.path.join(self.output_folder, 'package'), package_folder)
        tar.close()

    def _setup(self):
        # TODO(fanchi) - 18/Feb/2018: Parse package names and versions
        mkdir_p(self.output_folder)

        for pkg_name, order, ver in self.js_requirements:
            pkg_metadata = self._get_metadata(pkg_name)
            if not ver or ver == "latest":
                dist_tags = pkg_metadata.get('dist-tags')
                ver = dist_tags.get('latest', '0.0')
            version_info = semver.parse_version_info(ver)
            # TODO(fanchi) - 18/Feb/2018: Here should be the dependency parsing
            version_metadata = pkg_metadata.get('versions').get(ver)
            self._download_package(version_metadata, version_info)

        return self.options.created()

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


def _get_matching_dist_in_location(dist, location):
    """
    Check if `locations` contain only the one intended dist.
    Return the dist with metadata in the new location.
    """
    # Getting the dist from the environment causes the
    # distribution meta data to be read.  Cloning isn't
    # good enough.
    import pkg_resources
    env = pkg_resources.Environment([location])
    dists = [d for project_name in env for d in env[project_name]]
    dist_infos = [(d.project_name, d.version) for d in dists]
    if dist_infos == [(dist.project_name, dist.version)]:
        return dists.pop()
    if dist_infos == [(dist.project_name.lower(), dist.version)]:
        return dists.pop()


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise
