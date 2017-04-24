#!/usr/bin/env python
from flask import Flask, redirect, abort
import tempfile
import os, urllib, _thread, boto3, re, sys, time, traceback, json, requests
from datetime import datetime
from os.path import dirname, basename
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# Save logs of size 100MB, rotating out 30 of them
def init_logging(app, level=logging.INFO):
    if not os.path.isdir('/var/log/cache'):
        os.makedirs('/var/log/cache')
    handler = RotatingFileHandler('/var/log/cache/cache.log',
                                  maxBytes=100000000, backupCount=30)
    app.logger.addHandler(logging.StreamHandler())
    app.logger.setLevel(level)
    app.logger.addHandler(handler)

"""
url_name(url)

Return the "name" of an url.  Usually this is just `basename(url)` but in the
case of github tarball downloads, we combine the project name with the tag, e.g.
instead of turning something like `github.com/foo/bar/archive/v1.0.tar.gz`
into just `v1.0.tar.gz`, we turn it into `bar-v1.0.tar.gz`.  This is not a
functionality thing, it's an aesthetics thing, for when we download from the
cache and expect to get a reasonable filename, and matches the hints given by
github's servers, but which are lost when caching to S3 without this.
"""
def url_name(url):
    # special-case the name we calculate for github
    if "github" in url and (basename(dirname(url)) in ["archive", "tarball"]):
        return basename(dirname(dirname(url))) + "-" + basename(url)
    return basename(url)

class CacheEntry:
    """
    CacheEntry(cache, s3_obj)

    A CacheEntry is created one of two ways:
    * During a rebuild(), when we're walking the bucket and pulling our data out
      from .cache_data files
    * During an add(), when we've just uploaded to the bucket

    In either case, all the data we need to recreate this is stored within S3
    (and its magnificent "metadata" attribute) so that's all we pass the
    constructor in order to create a new one.
    """
    def __init__(self, s3_obj):
        # Save the S3 object so we can do things like remove ourselves
        self.s3_obj = s3_obj

        self.url = s3_obj.metadata['url']
        self.name = url_name(self.url)
        self.key = s3_obj.key

        # S3's etag is actually an MD5 sum, and we report it as such so that we
        # can verify checksums.  I wish it were a sha256, but we really don't
        # want to spend our precious cycles checksumming everything, so we use
        # what has been given us by the S3 gods.
        self.md5 = s3_obj.e_tag.strip('"')
        self.size = s3_obj.content_length
        self.modified = s3_obj.last_modified

        # We store the server etag (if we have one at all) in the S3 metadata
        if 'etag' in s3_obj.metadata:
            self.etag = s3_obj.metadata['etag'].strip('"')
        else:
            self.etag = None

        # We store our last consistency check time point, so we can throttle
        # those checks down a bit.
        self.last_consistency_check = 0

    def log(self, msg):
        global app
        app.logger.info("[%s] %s"%(self.name, msg))

    def delete(self):
        self.s3_obj.delete()
        self.cache.cache.remove(self.url)
        self.log("Deleted")

    def cache_url(self):
        return "https://julialangcache-s3.julialang.org/" + self.key

    def probe_headers(self):
        # HEAD the remote resource, failing out if it's not an HTTP 200 OK
        resp = requests.head(self.url, timeout=1, allow_redirects=True)
        if resp.status_code != 200:
            raise ValueError("Received HTTP %d for \"%s\""%(resp.status_code, url))

        # Grab the headers and inspect them for an ETag or Last-Modified entry,
        # as well as a content-type header
        headers = resp.headers

        etag = None
        if "etag" in headers:
            etag = headers["etag"].strip('"')

        last_modified = None
        if "last-modified" in headers:
            lm = headers["last-modified"]
            last_modified = datetime.strptime(lm, "%a, %d %b %Y %H:%M:%S %Z")

        # We're also going to look for a content-type header
        content_type = None
        if "content-type" in headers:
            content_type = headers["content-type"]

        return etag, last_modified, content_type

    def check_consistency(self):
        if self.url.startswith("ftp://"):
            self.log("Cannot consistency check FTP urls, serving cached file")
            return True

        # If we already have the file, we can quickly double-check that the file we
        # have cached is still consistent by checking ETag/Last-Modified times
        try:
            etag, last_modified, content_type = self.probe_headers()
        except:
            # If we run into an error during probe_headers(), we serve our
            # cached file to continue serving while the source server is awol
            self.log("Error while checking consistency, serving cached file")
            traceback.print_exc()
            return True

        # If the content_type is "text/html", just return "True" since some
        # sites (I'M LOOKING AT YOU SOURCEFORGE) will give back what looks like
        # a normal response (200 OK), but is, in fact, an error page.  We don't
        # cache html, because why on earth would you want to do that.
        if content_type == "text/html":
            return True

        # Do we have a stored ETag?
        if not self.etag is None:
            if etag is None:
                # We have a stored etag, but we didn't get one from the server.
                # Suspicious.  Move on to last-modified.
                self.log("ETag suddenly unavailable, checking Last-Modified")
                pass
            else:
                if etag != self.etag:
                    # We have a stored etag, and we got one from the server, but
                    # they didn't match.  Ring the alarm bells.
                    self.log("ETag changed! Old: %s, New: %s"%(self.etag, etag))
                    return False

                # We have a stored etag, we got one from the server, and they
                # matched.  That's good enough for us.
                self.log("Successfully validated ETag")
                return True

        # Do we have a last-modified date stored?
        if last_modified is None:
            self.log("Last-Modified unavailable, serving cached file")
            return True
        else:
            if last_modified > self.modified:
                flms = str(self.modified)
                lms = str(last_modified)
                self.log("Last-Modified changed! Old: %s, New: %s"%(flms, lms))
                return False
            else:
                self.log("Successfully validated Last-Modified")
                return True

        # If all probulations fail, just serve the cached file
        return True



class AWSCache:
    def __init__(self, bucket_name):
        # We maintain a connection to s3
        self.s3 = boto3.resource('s3')
        self.bucket_name = bucket_name

        # This is a mapping from URLs to CacheEntry's
        self.cache = {}
        self.rebuild()

    def rebuild(self):
        # This is the new dictionary we'll use to build up our cache
        new_cache = {}

        # List all our files
        bucket = self.s3.Bucket(self.bucket_name)
        objs = sorted(list(bucket.objects.all()), key = lambda o: o.key)

        # Construct CacheEntry objects for each object we've found
        for obj in objs:
            try:
                new_cache_entry = CacheEntry(self.s3.Object(self.bucket_name, obj.key))
                new_cache[new_cache_entry.url] = new_cache_entry
                app.logger.info("[%s] cache reloading object %s successful"%(new_cache_entry.url, obj.key))
            except:
                app.logger.warn("[%s] cache reload failed"%(obj.key))
                traceback.print_exc()
                pass

        # Finally, move new_aws_cache over to aws_cache, clearing out old stuff
        app.logger.info("Cache rebuild finished")
        self.cache = new_cache

    """
    url_to_key(url)

    Given the url of a file we wish to cache, calculate the key (e.g. the path
    within our upload bucket) at which it will be located.  This is done by
    hashing the dirname (everything before the last '/' character) of the given
    url, so that files located at different paths but with the same filename can
    be cached at the same time, effectively 'namespacing' files.
    """
    def url_to_key(self, url):
        from hashlib import sha256
        hash_dir = sha256(dirname(url).encode('utf-8')).hexdigest()

        # We must do the plus-to-space madness.  sigh.
        obj_name = basename(url).replace('+', ' ')
        return "%s/%s"%(hash_dir, obj_name)

    """
    add(url, local_filename)

    Given the remote URL and the local filename, add a previously-downloaded
    file's contents to the cache, uploading the file to S3 and inserting the
    requisite CacheEntry to our in-memory cache.
    """
    def add(self, url, local_filename, etag=None):
        obj = self.s3.Object(self.bucket_name, self.url_to_key(url))
        extra_args = {
            'ACL': 'public-read',
            'Metadata': {
                'url': url,
            }
        }
        if not etag is None:
            extra_args['Metadata']['etag'] = etag

        obj.upload_file(local_filename, ExtraArgs = extra_args)
        # Create the CacheEntry and add it into our in-memory cache listing
        self.cache[url] = CacheEntry(obj)

    def delete(self, url):
        if not url in self.cache:
            return
        self.cache[url].delete()

    def hit(self, url):
        return self.cache.get(url, None)


# This is our regex whitelist, listing URL patterns we will consent to caching
whitelist = [
    # Homebrew bottles
    "download.sf.net/project/machomebrew/Bottles",
    "homebrew.bintray.com/bottles",

    # WinRPM binaries.  This line is too long, but I don't care.  :/
    "download.opensuse.org/repositories/windows:/mingw:/win[\d]+/openSUSE_[\d\.]+/[^/]+",

    # Various deps/ tarball locations
    "faculty.cse.tamu.edu/davis/SuiteSparse",
    "download.savannah.gnu.org/releases/libunwind",
    "github.com/[^/]+/[^/]+/archive",
    "github.com/[^/]+/[^/]+/releases/download/([^/]+)?",
    "api.github.com/repos/[^/]+/[^/]+/tarball",
    "gmplib.org/download/gmp",
    "mpfr.org/mpfr-current",
    "mpfr.org/mpfr-[\d\.]+",
    "nixos.org/releases/patchelf/patchelf-[\d\.]+",
    "kernel.org/pub/software/scm/git",
    "pypi.python.org/packages/source/v/virtualenv",
    "llvm.org/releases/[\d\.]+",
    "math.sci.hiroshima-u.ac.jp/~m-mat/MT/SFMT",
    "agner.org/optimize",
    "netlib.org/lapack",
    "fftw.org",
    "unsis.googlecode.com/files",
    "intgat.tigress.co.uk/rmy/files/busybox",
    "frippery.org/files/busybox",
    "ftp.csx.cam.ac.uk/pub/software/programming/pcre",
    "bintray.com/artifact/download/[^/]+/generic",
    "imagemagick.org/download/binaries",
    "tls.mbed.org/download",
    "thrysoee.dk/editline",
    "ftp.atnf.csiro.au/pub/software/wcslib",

    # Add unicode fonts for libutf8
    "unicode.org/Public/UCD/latest/ucd/auxiliary",
    "unicode.org/Public/UNIDATA",
    "unifoundry.com/pub/unifont-[\d\.]+/font-builds",

    # Sourceforge URLs
    "sourceforge.net/projects/pcre/files/pcre/[^/]+",
    "downloads.sourceforge.net/sevenzip",
    "sourceforge.net/projects/juliadeps-win/files",

    # DLL file ZIPs for mbedTLS
    "api.github.com/repos/malmaud/malmaud.github.io/contents/files",
    "malmaud.github.io/files",

    # CMake binaries for JuliaLang/julia#19632
    "cmake.org/files/v[0-9\.]+",
]

# A list of regexes (NOT passed through regexify) that we reject
blacklist = [
    "favicon.ico",
]

# A list of regexes (NOT passed through regexify) that we refuse to cache, acts
# as a special exclusion list when we need to reject something that would
# otherwise be matched by the whitelist, and hence cached
greylist = [
    ".*/repomd.xml",
]

# Take an URL pattern and add all the regex stuff to match an incoming URL
def regexify(url):
    # Add http://, with optional https and www. in front.  Then, replace all
    # dots within the plain regex string with escaped dots, and finally add the
    # actual filename pattern at the end.
    return r"^(https?)|(ftp)://(www\.)?" + url.replace(r".", r"\.") + r"/[^/]+$"

whitelist = [w for w in map(regexify, whitelist)]

# The list of files that are currently downloading, so we don't download twice
pending_downloads = []

"""
add_to_cache(url)

Download the given url and add it to the cache, using `pending_downloads` to
prevent multiple simultaneous downloads of the same file.
"""
def add_to_cache(url):
    global pending_downloads, aws_cache
    # Stop double downloads if we get a flood of requests for a single file
    if url in pending_downloads:
        app.logger.info("[%s] Already downloading, skipping..."%(url))
        return
    pending_downloads += [url]

    # Download the requested file
    try:
        with tempfile.NamedTemporaryFile() as tmp_file:
            tmp_name = tmp_file.name
            tmp_name, headers = urllib.request.urlretrieve(url, tmp_name)

            # Beware, my children, of the false prophet Sourceforge, and his
            # bamboozling ways.  Accept not the gift of false downloads, and
            # suffer not the content-type of "text/html" to enter your caches.
            if headers.get("content-type", "") == "text/html":
                app.logger.info("[%s] Aborting, we got text/html back!"%(url))
                pending_downloads.remove(url)
                return

            # If nothing was downloaded, just exit out after cleaning up
            filesize = os.stat(tmp_name).st_size
            if filesize < 1024:
                app.logger.info("[%s] Aborting, filesize was <1k (%d)"%(url,filesize))
                pending_downloads.remove(url)
                return

            app.logger.info("[%s] Successfully finished download: %s (%dB)"%(url, tmp_name, filesize))
            aws_cache.add(url, tmp_name, headers.get("etag", None))

        pending_downloads.remove(url)
        app.logger.info("[%s] Finished upload"%(url))
    except IOError as e:
        # If we got a 404, clean up
        app.logger.info("[%s] Aborting, got 404"%(url))
        pending_downloads.remove(url)


# Asking for a full URL after <path:url> queries the cache
@app.route("/<path:url>")
def cache(url):
    global aws_cache, app

    # If this is a sourceforge url, and we're asking for something that ends in
    # /download, get rid of it; it's not necessary, and we can roll without it.
    # We also don't mind redirecting users to URLs without /download, even if
    # we don't cache it at all.
    if "sourceforge" in url and url[-9:] == "/download":
        url = url[:-9]

    if any([re.match(black_url, url) for black_url in blacklist]):
        app.logger.info("404'ing %s because it's on the blacklist"%(url))
        abort(404)

    # If it's on the greylist, just forward them on right now
    if any([re.match(grey_url, url) for grey_url in greylist]):
        app.logger.info("301'ing %s because it's on the greylist"%(url))
        return redirect(url, code=301)

    # Ensure this URL is something we want to deal with. If it's not, send the
    # user on their merry way to the original URL
    if not any([re.match(white_url, url) for white_url in whitelist]):
        app.logger.info("301'ing %s because it's not on the whitelist"%(url))
        return redirect(url, code=301)

    cache_entry = aws_cache.hit(url)
    # If we cache miss or we fail our consistency check, redownload the file
    if cache_entry is None or not cache_entry.check_consistency():
        # Start a thread downloading, but return immediately redirecting the
        # user temporarily to the original URL, until we've actually cached it.
        _thread.start_new_thread(add_to_cache, (url,))
        app.logger.info("302'ing %s because we need to redownload it"%(url))
        return redirect(url, code=302)

    # Otherwise, forward them on to the cache!
    app.logger.info("HIT: %s"%(url))
    return redirect(cache_entry.cache_url(), code=301)



# Fancyness!  Adapted from http://goo.gl/FrdBC0
def sizefmt(num, suffix='B'):
    for unit in ['','K','M','G','T','P','E','Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Y', suffix)

def ellipsize(name, max_len):
    if len(name) > max_len:
        # Keep short extensions
        ext = ''
        split_name = name
        while True:
            split_name, split_ext = os.path.splitext(split_name)
            # If we have no more extensions, then quit out
            if len(split_ext) == 0:
                break
            # If our extension is too long, then quit out
            if len(split_ext) + len(ext) > 8:
                break
            ext = split_ext + ext
        if not len(ext):
            return name[:max_len - 3 - len(ext)] + '...'
        else:
            return name[:max_len - 3 - len(ext)] + '...' + name[-len(ext):]
    return name

# Asking for nothing gives you the currently cached files
@app.route("/")
def index():
    html  = "<html>"
    html += "<head>"
    html +=     "<style>"
    html +=         "td { padding-right: 20px; }"
    html +=     "</style>"
    html += "</head>"
    html += "<body>"
    html += "Caching <b>%d</b> files:<br/><br/>\n"%(len(aws_cache.cache))
    html += "<table style=\"font-family: monospace;\">"
    URLs = sorted(aws_cache.cache.keys())
    for url in URLs:
        name = url_name(url)
        entry = aws_cache.hit(url)
        modified_str = entry.modified.strftime("%Y-%m-%d %H:%M:%S")

        html += "<tr>"
        html += "<td>"
        html += "["
        html += "<a href=\"%s\">cache</a>, "%(entry.cache_url())
        html += "<a href=\"/%s\">recache</a>, "%(entry.url)
        html += "<a href=\"%s\">source</a>"%(entry.url)
        html += "] <b>%s</b>"%(ellipsize(name, 35))
        html += "</td>"
        html += "<td>"
        html += "MD5:<br/><b>%s...</b></td>"%(entry.md5[:16])
        html += "<td>"
        html += "Modified:<br/><b>%s</b></td>\n"%(modified_str)
        html += "<td>"
        html += "Size:<br/><b>%s</b></td>\n"%(sizefmt(entry.size))
        html += "<td>"
        if not entry.etag is None:
            html += "ETag:<br/><b>%s</b>"%(ellipsize(entry.etag, 20))
        html += "</td>"
        html += "</tr>"

    html += "</table>"
    html += "</body>"
    html += "</html>"
    return html

if __name__ == "__main__":
    init_logging(app)

    # Initialize aws_cache
    aws_cache = AWSCache("julialangcache")

    app.run(host="0.0.0.0",threaded=True)
