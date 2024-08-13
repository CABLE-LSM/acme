"""Client object."""

import requests
import hashlib as hl
import os
from typing import Union
from urllib.parse import urljoin
from meorg_client.exceptions import RequestException
import meorg_client.constants as mcc
import meorg_client.endpoints as endpoints
import meorg_client.exceptions as mx
import meorg_client.utilities as mu
import mimetypes as mt
from pathlib import Path
from multiprocessing import Pool


class Client:
    def __init__(self, email: str = None, password: str = None, dev_mode: bool = False):
        """ME.org Client object.

        Supplying email and password will automatically log in.

        Parameters
        ----------
        base_url : str
            Base URL to API.
        email : str, optional
            Registered email address, by default None
        password : str, optional
            User password, by default None
        dev_mode : bool, optional
            Development mode (uses dev environment), by default False
        """

        # Initialise the mimetypes
        mt.init()

        # Dev mode can be set by the user or from the environment
        dev_mode = dev_mode or os.getenv("MEORG_DEV_MODE", "0") == "1"

        if dev_mode:
            self.base_url = os.getenv("MEORG_BASE_URL_DEV", None)
        else:
            self.base_url = mcc.MEORG_BASE_URL_PROD

        self.headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        self.last_response = None

        # Automatically login if credentials are set.
        if email is not None and password is not None:
            self.login(email, password)

    def _make_request(
        self,
        method: str,
        endpoint: str,
        url_params: dict = {},
        data: dict = {},
        json: dict = {},
        headers: dict = {},
        files: dict = {},
        return_json=True,
        **kwargs,
    ):
        """Make a request against the API

        Parameters
        ----------
        method : str
            HTTP method.
        endpoint : str
            URL template for the API endpoint.
        url_params : dict, optional
            Parameters to interpolate into the URL template, by default {}
        data : dict, optional
            Data to send along with the request, by default {}
        json : dict, optional
            JSON data to send along with the request, by default {}
        headers : dict, optional
            Headers to attach to the request (will be combined with client headers), by default {}
        files : dict, optional
            Files payload to attach to request, by default {}
        return_json : bool, optional
            Return a JSON dict object, by default True

        Returns
        -------
        dict or requests.Response
            Dictionary or Request object, depending on context.

        Raises
        ------
        mx.InvalidHTTPMethodException
            Raised when the specified method is invalid.
        RequestException
            Raised when the request fails.
        """

        method = method.upper()

        # Check that the method is allowed.
        if method not in mcc.VALID_METHODS:
            raise mx.InvalidHTTPMethodException(method)

        # Get the function and URL
        func = getattr(requests, method.lower())
        url = self._get_url(endpoint, **url_params)

        # Assemble the headers
        _headers = self._merge_headers(headers)

        # Make the request, set it as the last response for future use
        self.last_response = func(
            url, data=data, json=json, headers=_headers, files=files, **kwargs
        )

        # Check to see if it was successful
        if self.last_response.status_code not in mcc.HTTP_STATUS_SUCCESS_RANGE:
            raise RequestException(
                self.last_response.status_code, self.last_response.text
            )

        # This is the default
        if return_json:
            return self.last_response.json()

        # For flexibility
        return self.last_response

    def _get_url(self, endpoint: str, **kwargs):
        """Get the well-formed URL for the call.

        Parameters
        ----------
        endpoint : str
            Endpoint to be appended to the base URL.
        **kwargs :
            Key/value pairs to interpolate into the URL template.

        Returns
        -------
        str
            URL.
        """
        return urljoin(self.base_url + "/", endpoint).format(**kwargs)

    def _merge_headers(self, headers: dict = dict()):
        """Merge additional headers into the client headers (i.e. Auth)

        Parameters
        ----------
        headers : dict, optional
            Additional headers to add to the client headers, by default dict()

        Returns
        -------
        dict
            Merged headers.
        """
        return {**self.headers, **headers}

    def login(self, email: str, password: str):
        """Log the user into ME.org.

        Parameters
        ----------
        email : str
            Registered email address.
        password : str
            Password (will be hashed)

        Raises
        ------
        Exception
            When the login fails.
        """

        # Assemble payload
        login_data = {
            "email": email,
            "password": hl.sha256(password.encode("UTF-8")).hexdigest(),
            "hashed": "true",
        }

        # Call
        response = self._make_request(
            method=mcc.HTTP_POST,
            endpoint=endpoints.LOGIN,
            json=login_data,
            return_json=True,
        )

        # Successful login
        if self.last_response.status_code == 200:
            auth_headers = {
                "X-User-Id": response["data"]["userId"],
                "X-Auth-Token": response["data"]["authToken"],
            }

            self.headers.update(auth_headers)

        # Unsuccessful login (technically this will have already failed)
        else:
            raise RequestException(
                self.last_response.status_code, self.last_response.text
            )

    def logout(self):
        """Log the user out. Likely not necessary, can just let sessions expire."""
        response = self._make_request(
            method=mcc.HTTP_POST, endpoint=endpoints.LOGOUT, return_json=False
        )

        # Clear the headers.
        if response.status_code == 200:
            self.headers.pop("X-User-Id", None)
            self.headers.pop("X-Auth-Token", None)

    def upload_files_parallel(self, files: Union[str, Path, list], n: int = 2):
        """Upload files in parallel.

        Parameters
        ----------
        files : Union[str, Path, list]
            A path to a file, or a list of paths.
        n : int, optional
            Number of threads to use, by default 2.

        Returns
        -------
        list
            List of dicts or response objects from upload_files.
        """

        # Ensure the object is actually iterable
        files = mu.ensure_list(files)

        # Single file provided, don't bother starting the pool
        if len(files) == 1:
            return self.upload_files(files)

        # Do the parallel upload
        responses = None
        with Pool(processes=n) as pool:
            responses = pool.map(self.upload_files, files)

        return responses

    def upload_files(
        self,
        files: Union[str, Path],
        attach_to: str = None,
    ) -> Union[dict, requests.Response]:
        """Upload a file.

        Parameters
        ----------
        files : path-like, list
            Path to the file, or a list containing paths.
        attach_to : str, optional
            Optional model_output_id to attach the files to, by default None

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.

        Raises
        ------
        TypeError
            When supplied file(s) are neither path-like nor readable.
        FileNotFoundError
            When supplied file(s) cannot be found.
        """

        # Cast as list for iterative upload
        files = mu.ensure_list(files)

        # Prepare the files
        _files = list()
        for ix, f in enumerate(files):
            # Path-like
            if isinstance(f, (str, Path)) and os.path.isfile(f):
                _files.append(open(f, "rb"))

            # Bail out
            else:
                dtype = type(f)
                raise TypeError(
                    f"File at index {ix} is neither path-like nor readable ({dtype})."
                )

        # Prepare the payload from the files
        payload = list()

        for _f in _files:
            filename = os.path.basename(_f.name)
            ext = filename.split(".")[-1]
            mimetype = mt.types_map[f".{ext}"]
            payload.append(("file", (filename, _f, mimetype)))

        # Make the request
        response = self._make_request(
            method=mcc.HTTP_POST,
            endpoint=endpoints.FILE_UPLOAD,
            files=payload,
            return_json=True,
        )

        # Close all the file descriptors (requests should do this, but just to be sure)
        for fd in payload:
            fd[1][1].close()

        # Automatically attach to a model output
        if attach_to:
            response = self.attach_files_to_model_output(
                attach_to, files=mu.get_uploaded_file_ids(response)
            )

        return response

    def list_files(self, id: str) -> Union[dict, requests.Response]:
        """Get a list of model outputs.

        Parameters
        ----------
        id : str
            Model output ID

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.
        """
        return self._make_request(
            method=mcc.HTTP_GET, endpoint=endpoints.FILE_LIST, url_params=dict(id=id)
        )

    def attach_files_to_model_output(
        self, id: str, files: list
    ) -> Union[dict, requests.Response]:
        """Attach files to a model output.

        Parameters
        ----------
        id : str
            Model output ID.
        files : list
            List of file IDs.

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.
        """

        # Get a list of files for the model output
        current_files = self.list_files(id).get("data").get("files")

        # Attach the new files to this list
        new_files = current_files + files

        # Update the resource
        return self._make_request(
            mcc.HTTP_PATCH,
            endpoint=endpoints.FILE_LIST,
            url_params=dict(id=id),
            json=new_files,
        )

    def start_analysis(self, id: str) -> Union[dict, requests.Response]:
        """Start the analysis chain.

        Parameters
        ----------
        id : str
            Model output ID.

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.
        """
        return self._make_request(
            method=mcc.HTTP_PUT,
            endpoint=endpoints.ANALYSIS_START,
            url_params=dict(id=id),
        )

    def get_analysis_status(self, id: str) -> Union[dict, requests.Response]:
        """Check the status of the analysis chain.

        Parameters
        ----------
        id : str
            Analysis ID.

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.
        """
        return self._make_request(
            method=mcc.HTTP_GET,
            endpoint=endpoints.ANALYSIS_STATUS,
            url_params=dict(id=id),
        )

    def list_endpoints(self) -> Union[dict, requests.Response]:
        """List the endpoints available to the user.

        Paths are available at .get('paths').keys()

        Returns
        -------
        Union[dict, requests.Response]
            Response from ME.org.
        """
        return self._make_request(method=mcc.HTTP_GET, endpoint=endpoints.ENDPOINT_LIST)

    def success(self) -> bool:
        """Test if the last request was successful.

        Returns
        -------
        bool
            True if successful, False otherwise.
        """
        return self.last_response.status_code in mcc.HTTP_STATUS_SUCCESS_RANGE
