import os
import asyncio

from waterbutler.core import utils
from waterbutler.core import streams
from waterbutler.core import provider
from waterbutler.core import exceptions

from waterbutler.providers.box import settings
from waterbutler.providers.box.metadata import BoxRevision
from waterbutler.providers.box.metadata import BoxFileMetadata
from waterbutler.providers.box.metadata import BoxFolderMetadata


class BoxPath(utils.WaterButlerPath):

    def __init__(self, folder, path, prefix=True, suffix=False):
        super().__init__(path, prefix=prefix, suffix=suffix)

        self._folder = folder

        full_path = os.path.join(folder, path.lstrip('/'))
        self._full_path = self._format_path(full_path)

    def __repr__(self):
        return "{}({!r}, {!r})".format(self.__class__.__name__, self._folder, self._orig_path)

    @property
    def full_path(self):
        return self._full_path


class BoxProvider(provider.BaseProvider):

    BASE_URL = settings.BASE_URL
    BASE_CONTENT_URL = settings.BASE_CONTENT_URL

    def __init__(self, auth, credentials, settings):
        super().__init__(auth, credentials, settings)
        self.token = self.credentials['token']
        self.folder = self.settings['folder']

    @property
    def default_headers(self):
        return {
            'Authorization': 'Bearer {}'.format(self.token),
        }

    @asyncio.coroutine
    def intra_copy(self, dest_provider, source_options, dest_options):
        source_path = BoxPath(self.folder, source_options['path'])
        dest_path = BoxPath(self.folder, dest_options['path'])
        if self == dest_provider:
            resp = yield from self.make_request(
                'POST',
                self.build_url('fileops', 'copy'),
                data={
                    'folder': 'auto',
                    'from_path': source_path.full_path,
                    'to_path': dest_path.full_path,
                },
                expects=(200, 201),
                throws=exceptions.IntraCopyError,
            )
        else:
            from_ref_resp = yield from self.make_request(
                'GET',
                self.build_url('copy_ref', 'auto', source_path.full_path),
            )
            from_ref_data = yield from from_ref_resp.json()
            resp = yield from self.make_request(
                'POST',
                data={
                    'root': 'auto',
                    'from_copy_ref': from_ref_data['copy_ref'],
                    'to_path': dest_path,
                },
                headers=dest_provider.default_headers,
                expects=(200, 201),
                throws=exceptions.IntraCopyError,
            )
        data = yield from resp.json()
        return BoxFileMetadata(self.folder, data).serialized()

    @asyncio.coroutine
    def intra_move(self, dest_provider, source_options, dest_options):
        source_path = BoxPath(self.folder, source_options['path'])
        dest_path = BoxPath(self.folder, dest_options['path'])
        resp = yield from self.make_request(
            'POST',
            self.build_url('fileops', 'move'),
            data={
                'root': 'auto',
                'from_path': source_path.full_path,
                'to_path': dest_path.full_path,
            },
            expects=(200, ),
            throws=exceptions.IntraMoveError,
        )
        data = yield from resp.json()
        return BoxFileMetadata(data, self.folder).serialized()

    @asyncio.coroutine
    def download(self, path, revision=None, **kwargs):
        path = BoxPath(self.folder, path)
        resp = yield from self.make_request(
            'GET',
            self._build_content_url('files', 'auto', path.full_path),
            expects=(200, ),
            throws=exceptions.DownloadError,
        )
        return streams.ResponseStreamReader(resp)

    @asyncio.coroutine
    def upload(self, stream, path, **kwargs):
        path = BoxPath(self.folder, path)

        try:
            yield from self.metadata(str(path))
        except exceptions.MetadataError:
            created = True
        else:
            created = False

        resp = yield from self.make_request(
            'PUT',
            self._build_content_url('files_put', 'auto', path.full_path),
            headers={'Content-Length': str(stream.size)},
            data=stream,
            expects=(200, ),
            throws=exceptions.UploadError,
        )

        data = yield from resp.json()
        return BoxFileMetadata(data, self.folder).serialized(), created

    @asyncio.coroutine
    def delete(self, path, **kwargs):
        path = BoxPath(self.folder, path)

        # A metadata call will verify the path specified is actually the
        # requested file or folder.
        yield from self.metadata(str(path))

        yield from self.make_request(
            'POST',
            self.build_url('fileops', 'delete'),
            data={'root': 'auto', 'path': path.full_path},
            expects=(200, ),
            throws=exceptions.DeleteError,
        )

    @asyncio.coroutine
    def metadata(self, path, **kwargs):
        path = BoxPath(self.folder, path)

        resp = yield from self.make_request(
            'GET',
            self.build_url('metadata', 'auto', path.full_path),
            expects=(200, ),
            throws=exceptions.MetadataError
        )
        data = yield from resp.json()

        # Dropbox will match a file or folder by name within the requested path. Box may or may not
        if path.is_file and data['is_dir']:
            raise exceptions.MetadataError(
                'Could not retrieve file \'{0}\''.format(path),
                code=404,
            )

        if data.get('is_deleted'):
            if data['is_dir']:
                raise exceptions.MetadataError(
                    'Could not retrieve folder \'{0}\''.format(path),
                    code=404,
                )
            raise exceptions.MetadataError(
                'Could not retrieve file \'{0}\''.format(path),
                code=404,
            )

        if data['is_dir']:
            ret = []
            for item in data['contents']:
                if item['is_dir']:
                    ret.append(BoxFolderMetadata(item, self.folder).serialized())
                else:
                    ret.append(BoxFileMetadata(item, self.folder).serialized())
            return ret

        return BoxFileMetadata(data, self.folder).serialized()

    @asyncio.coroutine
    def revisions(self, path, **kwargs):
        path = BoxPath(self.folder, path)
        response = yield from self.make_request(
            'GET',
            self.build_url('revisions', 'auto', path.full_path),
            expects=(200, ),
            throws=exceptions.RevisionError
        )
        data = yield from response.json()

        return [
            BoxRevision(item).serialized()
            for item in data
        ]

    def can_intra_copy(self, dest_provider):
        return type(self) == type(dest_provider)

    def can_intra_move(self, dest_provider):
        return self.can_intra_copy(dest_provider)

    def _build_content_url(self, *segments, **query):
        return provider.build_url(settings.BASE_CONTENT_URL, *segments, **query)
