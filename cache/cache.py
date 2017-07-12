#!/usr/bin/env python
from flask import Flask, redirect, abort, Response
import tempfile
import os, urllib, _thread, boto3, re, sys, time, traceback, json
from datetime import datetime
from dateutil.tz import tzutc
from os.path import dirname, basename
import logging
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# Save logs of size 100MB, rotating out 30 of them
def init_logging(app, logdir='/var/log/cache'):
    if not os.path.isdir(logdir):
        os.makedirs(logdir)
    # We set the top logger to INFO so that all necessary messages are processed
    app.logger.setLevel(logging.INFO)

    # This is where we'll store all the messages
    all_path = logdir + '/cache.log'
    all_handler = RotatingFileHandler(all_path, maxBytes=1e8, backupCount=30)
    all_handler.setLevel(logging.INFO)
    app.logger.addHandler(all_handler)

    # But errors will be specifically brought over here
    err_path = logdir + '/cache.err.log'
    err_handler = RotatingFileHandler(err_path, maxBytes=1e8, backupCount=30)
    err_handler.setLevel(logging.ERROR)
    app.logger.addHandler(err_handler)

    # Log INFO's and above out to stdout
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(logging.INFO)
    app.logger.addHandler(stdout_handler)


"""
log(msg, level)

Log the given message out to the app's logger instance.  This will get written
to the main log file and written out to stdout, and if it's of level ERROR or
higher, it'll get logged to the special error log as well.

We automagically prepend things like the current time to give the illusion of
order amidst the chaos of our logfile.
"""
def log(msg, level=logging.INFO):
    global app
    time_str = datetime.now().strftime("%d/%b/%Y %H:%M:%S")
    app.logger.log(level, "[%s] %s"%(time_str, msg))


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

        # This is all transient data, things we do not persist within S3.  We
        # We store some statistics so that we can debug issues a bit, and also
        # throttle down our number of consistency checks, but these are all
        # transient; we do not persist these in S3, we just start from zero
        # again when we restart the app.
        self.consistent = False
        self.last_consistency_check = 0
        self.last_successful_consistency_check = 0
        self.consistency_checks = 0
        self.consecutive_unsuccessful_consistency_checks = 0
        # ^^ What a travesty of a variable name.  I love it.

    def log(self, msg):
        global app
        log("[%s] %s"%(self.name, msg))

    def delete(self):
        self.s3_obj.delete()
        self.log("Deleted")

    def cache_url(self):
        # TEMPORARILY DISABLE FASTLY BECAUSE OF CENTOS 5 BUILDBOT SSL PROBLEM
        # https://github.com/JuliaLang/julia/pull/21684#issuecomment-298812729
        # https://github.com/JuliaWeb/MbedTLS.jl/issues/102#issuecomment-298265305
        #return "https://julialangcache-s3.julialang.org/" + self.key
        #
        # Use S3 directly instead, until we can move to Centos 6 on the buildbots
        return "https://julialangcache.s3.amazonaws.com/" + self.key

    def probe_headers(self):
        # HEAD the remote resource, failing out if it's not an HTTP 200 OK
        req = urllib.request.Request(self.url, method="HEAD")
        resp = urllib.request.urlopen(req, timeout=1.5)
        if resp.code != 200:
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
            # Normalize everything to UTC
            last_modified = last_modified.astimezone(tzutc())

        # We're also going to look for a content-type header
        content_type = None
        if "content-type" in headers:
            content_type = headers["content-type"]

        return etag, last_modified, content_type

    def _check_consistency(self):
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

    """
    check_consistency()

    Returns `True` if the server responds with metadata about the cached file
    (such as an `ETag` or `Last-Modified` header) that ensures to us that our
    cached version of the file is still consistent with the live version on the
    server.  Note that the result of this consistency check is, by default,
    cached for 1 minute to avoid flooding upstream servers with HEAD requests.
    """
    def check_consistency(self, cache_time = 1*60):
        # First, check to see if we shouldn't just return our cached consistency
        curr_time = time.time()
        if curr_time - self.last_consistency_check < cache_time:
            return self.consistent

        # Otherwise, ask for the consistency
        self.last_consistency_check = curr_time
        self.consistency_checks += 1
        self.consistent = self._check_consistency()

        # We keep track of some basic statistics on consistency
        if self.consistent:
            self.last_successful_consistency_check = curr_time
            self.consecutive_unsuccessful_consistency_checks = 0
        else:
            self.consecutive_unsuccessful_consistency_checks += 1

        # This is what it's all about.  This is why we do all this.  For the
        # consistency Morty, for the consistency!
        return self.consistent

    """
    json_obj()

    Returns a json-serializable dict that summarizes this CacheEntry
    """
    def json_obj(self):
        return {
            'name': self.name,
            'size': self.size,
            'key': self.key,
            'md5': self.md5,
            'etag': self.etag,
            'modified': self.modified.timestamp(),
            'consistency' : {
                'last_check': self.last_consistency_check,
                'last_good_check': self.last_successful_consistency_check,
                'num_checks': self.consistency_checks,
                'bad_streak': self.consecutive_unsuccessful_consistency_checks,
            },
        }



class AWSCache:
    def __init__(self, bucket_name):
        # We maintain a connection to s3
        self.s3 = boto3.resource('s3')
        self.bucket_name = bucket_name

        # This is a mapping from URLs to CacheEntry's
        self.cache = {}
        self.rebuild()

        self.start_time = time.time()
        self.total_hits = 0


    def rebuild(self):
        # This is the new dictionary we'll use to build up our cache
        new_cache = {}

        # Let's keep track of how long it takes to do this
        start_time = time.time()

        # List all our files
        bucket = self.s3.Bucket(self.bucket_name)
        objs = sorted(list(bucket.objects.all()), key = lambda o: o.key)

        # Construct CacheEntry objects for each object we've found
        for obj in objs:
            try:
                new_cache_entry = CacheEntry(self.s3.Object(self.bucket_name, obj.key))
                new_cache[new_cache_entry.url] = new_cache_entry
                log("[%s] cache reloading object %s successful"%(new_cache_entry.url, obj.key))
            except:
                log("[%s] cache reload failed"%(obj.key), level=logging.WARN)
                traceback.print_exc()
                pass

        # Finally, move new_cache over to self.cache, clearing out old stuff,
        # and not disrupting our uptime one iota
        log("Cache rebuild finished in %.1fs"%(time.time() - start_time))
        self.cache = new_cache

    """
    check_cache_consistency()

    Performs various sanity checks on the cache, things like the fact that all
    cache objects are actually enabled on the whitelist/not black or greylisted,
    that all the files we are currently caching are current, etc...
    """
    def check_cache_consistency(self):
        for url in self.cache:
            log("[%s] Checking consistency"%(url))
            entry = self.cache[url]

            if on_blacklist(url):
                log("  [%s] Cached file is blacklisted!"%(url), level=logging.WARN)
            if on_greylist(url):
                log("  [%s] Cached file is greylisted!"%(url), level=logging.WARN)
            if not on_whitelist(url):
                log("  [%s] Cached file is not whitelisted!"%(url), level=logging.WARN)

            try:
                if not entry.check_consistency():
                    log("  [%s] Cached file is stale"%(url), level=logging.WARN)
            except IOError as err:
                log("  [%s] Consistency check timed out"%(url), level=logging.WARN)

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
        del self.cache[url]

    def hit(self, url):
        self.total_hits += 1
        return self.cache.get(url, None)

    """
    json_obj(self)

    Returns a json-serializable dict that summarizes this Cache and every
    contained CacheEntry
    """
    def json_obj(self):
        objs = {url: self.cache[url].json_obj() for url in self.cache.keys()}
        return {
            'uptime': time.time() - self.start_time,
            'total_hits': self.total_hits,
            'cache_entries': objs,
        }


# This is our regex whitelist, listing URL patterns we will consent to caching
whitelist = [
    # Homebrew bottles
    "download.sf.net/project/machomebrew/Bottles",
    "homebrew.bintray.com/bottles",

    # WinRPM binaries.  This line is too long, but I don't care.  :/
    "download.opensuse.org/repositories/windows:/mingw:/win[\d]+/openSUSE_[\d\.]+/[^/]+",

    # Stuff we put on our julialang S3 buckets
    "s3.amazonaws.com/julialang[\w/\d]*",
    "julialang[\w\-\d]*.s3.amazonaws.com/",

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
    "releases.llvm.org/[\d\.]+",
    "math.sci.hiroshima-u.ac.jp/~m-mat/MT/SFMT",
    "agner.org/optimize",
    "netlib.org/lapack",
    "fftw.org",
    "unsis.googlecode.com/files",
    "storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/unsis",
    "intgat.tigress.co.uk/rmy/files/busybox",
    "frippery.org/files/busybox",
    "ftp.csx.cam.ac.uk/pub/software/programming/pcre",
    "bintray.com/artifact/download/[^/]+/generic",
    "imagemagick.org/download/binaries",
    "tls.mbed.org/download",
    "thrysoee.dk/editline",
    "ftp.atnf.csiro.au/pub/software/wcslib",
    "curl.haxx.se/download",

    # Add unicode fonts for libutf8
    "unicode.org/Public/UCD/latest/ucd/auxiliary",
    "unicode.org/Public/UNIDATA",
    "unifoundry.com/pub/unifont-[\d\.]+/font-builds",
    "unicode.org/Public/[\d\.]+/ucd",

    # Sourceforge URLs
    "sourceforge.net/projects/pcre/files/pcre/[^/]+",
    "downloads.sourceforge.net/sevenzip",
    "sourceforge.net/projects/juliadeps-win/files",

    # DLL file ZIPs for mbedTLS
    "api.github.com/repos/malmaud/malmaud.github.io/contents/files",
    "malmaud.github.io/files",

    # Test matrix
    "raw.githubusercontent.com/opencollab/arpack-ng/[\d.]+/TESTS",

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
    return r"^((https?)|(ftp))://(www\.)?" + url.replace(".", "\.") + r"/[^/]+$"

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
        log("[%s] Already downloading, skipping..."%(url))
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
                log("[%s] Aborting, we got text/html back!"%(url))
                pending_downloads.remove(url)
                return

            # If nothing was downloaded, just exit out after cleaning up
            filesize = os.stat(tmp_name).st_size
            if filesize < 1024:
                log("[%s] Aborting, filesize was <1k (%d)"%(url, filesize))
                pending_downloads.remove(url)
                return

            log("[%s] Successfully finished download: %s (%dB)"%(url, tmp_name, filesize))
            aws_cache.add(url, tmp_name, headers.get("etag", None))

        pending_downloads.remove(url)
        log("[%s] Finished upload"%(url))
    except IOError as e:
        # If we got a 404, clean up
        log("[%s] Aborting, got 404"%(url))
        pending_downloads.remove(url)

"""
on_blacklist(url)

Returns true if the given URL is on the blacklist (and should be 404'ed)
"""
def on_blacklist(url):
    global blacklist
    return any([re.match(black_url, url) for black_url in blacklist])

"""
on_greylist(url)

Returns true if the given URL is on the greylist (and should be expressly 301'ed
on to the source URL)
"""
def on_greylist(url):
    global greylist
    return any([re.match(grey_url, url) for grey_url in greylist])

"""
on_whitelist(url)

Returns true of the given URL is on the whitelist (and thus can be cached)
"""
def on_whitelist(url):
    global whitelist
    return any([re.match(white_url, url) for white_url in whitelist])


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

    if on_blacklist(url):
        log("[%s] 404'ing because it's on the blacklist"%(url))
        abort(404)

    # If it's on the greylist, or not on the whitelist, just forward them on
    # to the source url immediately, because we won't cache those links
    if on_greylist(url) or not on_whitelist(url):
        log("[%s] 301'ing to source because it's greylisted or at least not whitelisted"%(url))
        return redirect(url, code=301)

    cache_entry = aws_cache.hit(url)
    # If we cache miss or we fail our consistency check, redownload the file
    if cache_entry is None or not cache_entry.check_consistency():
        # Start a thread downloading, but return immediately redirecting the
        # user temporarily to the original URL, until we've actually cached it.
        _thread.start_new_thread(add_to_cache, (url,))
        log("[%s] 302'ing because we need to freshen up"%(url))
        return redirect(url, code=302)

    # Otherwise, forward them on to the cache!
    log("[%s] HIT!"%(url))
    return redirect(cache_entry.cache_url(), code=301)



# Fancyness!  Adapted from http://goo.gl/FrdBC0
"""
sizefmt(num, suffix='B')

Given a number, return a `%%.1f` representation of that number with an SI suffix
such as "GB" for GigaBytes, or if you provide `Hz` as the `suffix`, `GHz`, etc.
"""
def sizefmt(num, suffix='B'):
    for unit in ['','K','M','G','T','P','E','Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Y', suffix)

"""
ellipsize(name, max_len)

Given a string `name` and the maximum length `max_len` you want to support,
return a string truncated if it is greater than `max_len` with ellipses at the
end.  Note that if the string is filename-like (e.g. ends with an extension
such as `.tar.gz`) the extension will be preserved if it is not too long.

Example: ellipsize("this_is_long.tar.gz", 17)  ->  'this_is_...tar.gz'
"""
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
            return name[:max_len - 2 - len(ext)] + '..' + name[-len(ext):]
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

    num_files = len(aws_cache.cache)
    total_size = sum(aws_cache.cache[k].size for k in aws_cache.cache)
    html += "Caching <b>%d</b> files, "%(num_files)
    html += "totalling <b>%s</b>:"%(sizefmt(total_size))
    html += "<br/><br/>"
    html += "<table style=\"font-family: monospace;\">"

    lower_name = lambda url: aws_cache.cache[url].name.lower()
    URLs = sorted(aws_cache.cache.keys(), key = lambda url: lower_name(url))
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

@app.route("/api/json")
def json_dump():
    global aws_cache
    json_data = json.dumps(aws_cache.json_obj())
    return Response(json_data, mimetype="application/json")

if __name__ == "__main__":
    init_logging(app)

    # Initialize aws_cache
    aws_cache = AWSCache("julialangcache")

    # This is a good debugging check
    #aws_cache.check_cache_consistency()

    app.run(host="0.0.0.0",threaded=True)
