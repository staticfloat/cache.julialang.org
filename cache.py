#!/usr/bin/env python
from flask import Flask, redirect, abort
import os, urllib, thread, boto, re, sys, httplib2, time, traceback
from datetime import datetime
from os.path import dirname, basename
app = Flask(__name__)

# Let's unbuffer sys.stdout so that when we print out debugging messages, they appear immediately
class Unbuffered(object):
   def __init__(self, stream):
	   self.stream = stream
   def write(self, data):
	   self.stream.write(data)
	   self.stream.flush()
   def __getattr__(self, attr):
	   return getattr(self.stream, attr)
sys.stdout = Unbuffered(sys.stdout)

# This is the list of files we have successfully cached in the past and can spit out immediately
# We will cache information about each cached file as well, such as its SHA, etc...
aws_cache = {}

# This is the list of files that are currently downloading, so we don't download it twice
pending_cache = []

# This is our regex whitelist, listing what URL patterns we will consent to caching
whitelist = [
	# Homebrew bottles
	"download.sf.net/project/machomebrew/Bottles",
	"homebrew.bintray.com/bottles",

	# WinRPM binaries
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

	# Add unicode fonts for libutf8
	"unicode.org/Public/UCD/latest/ucd/auxiliary",
	"unicode.org/Public/UNIDATA",
	"unifoundry.com/pub/unifont-[\d\.]+/font-builds",

	# Sourceforge URLs, which I am significantly happier with now that I realized we can omit /download
	"sourceforge.net/projects/pcre/files/pcre/[^/]+",
	"downloads.sourceforge.net/sevenzip",
	"sourceforge.net/projects/juliadeps-win/files",

	# DLL file ZIPs for mbedTLS
    "api.github.com/repos/malmaud/malmaud.github.io/contents/files",
	"malmaud.github.io/files",
]

# A list of regexes (that are NOT passed through regexify) that we reject out of hand
blacklist = [
	"favicon.ico",
]

# A list of regexes (that are NOT passed through regexify) that we refuse to cache,
# acts as a special exclusion list when we need something that would otherwise be matched by the whitelist
greylist = [
	".*/repomd.xml",
]

# Take a stripped-down URL and add all the regex stuff to make it something we'd have dinner with
def regexify(url):
	# Add http://, with optional https and www. in front.  Then, replace all dots within the plain
	# regex string with escaped dots, and finally add the actual filename pattern at the end.
	return r"^https?://(www\.)?" + url.replace(r".", r"\.") + r"/[^/]+$"

whitelist = map(regexify, whitelist)


# urllib.urlretrieve() doesn't throw errors on 404 by default
class WhyOhWhyDontYouThrowErrorsUrlretrieve(urllib.FancyURLopener):
  def http_error_default(self, url, fp, errcode, errmsg, headers):
	urllib.URLopener.http_error_default(self, url, fp, errcode, errmsg, headers)

def add_to_cache(url, name):
	global pending_cache, aws_cache
	# Stop bad things from happening if we get a flood of requests for a single file
	if name in pending_cache:
		return
	pending_cache += [name]

	print "[%s] Starting download"%(name)

	# Download the requested file
	try:
		opener = WhyOhWhyDontYouThrowErrorsUrlretrieve()
		tmp_name, headers = opener.retrieve(url)

		# If nothing was downloaded, just exit out after cleaning up
		filesize = os.stat(tmp_name).st_size
		if filesize == 0:
			pending_cache.remove(name)
			return

		print "[%s] Finished download: %s (%d bytes)"%(name, tmp_name, filesize)

		# Login to S3
		conn = boto.connect_s3()
		bucket = conn.get_bucket("juliacache")

		# Store the file to S3, but replace '+'' characters with ' '
		k = boto.s3.key.Key(bucket)
		k.key = name.replace('+', ' ')
		k.set_contents_from_filename(tmp_name)
		k.set_acl('public-read')
		k.close()

		# Finally, add this name into our aws_cache, and remove it from pending_cache
		k = bucket.get_key(k.key)

		# Remember that the etag give to us by S3 is _not_ the same as the .etag file we store
		aws_cache[name] = {"MD5":k.etag, "size":filesize, "modified": boto.utils.parse_ts(k.last_modified)}

		# If the server gives us an etag, store that in its own file as well
		if "etag" in headers:
			aws_cache[name]["etag"] = headers["etag"].strip('"')
			k = boto.s3.key.Key(bucket)
			k.key = name + ".etag"
			k.set_contents_from_string(aws_cache[name]["etag"])
			k.set_acl('public-read')
			k.close()

		pending_cache.remove(name)
		print "[%s] Finished upload"%(name)
	except IOError as e:
		# If we got a 404, clean up
		print "[%s] 404, halting"%(name)
		pending_cache.remove(name)


def remove_from_cache(name):
	print "[%s] Removing from cache"%(name)
	del aws_cache[name]

	# Login to S3
	conn = boto.connect_s3()
	bucket = conn.get_bucket("juliacache")
	bucket.delete_key(name)

	# Delete a .etag file on the off chance that it exists (doesn't throw an error)
	bucket.delete_key(name + ".etag")


# Do a HEAD request for the file, getting the ETag and Last-Modified headers,
# returning None for either if we can't get those headers
def probe_etag_and_modified(url):
	# If we're dealing with github, we need to ask codeload to avoid downloading the
	# entire file again.  I think this might be a bug in how python handles redirects
	if not "codeload" in url:
		url.replace("/github.com/", "/codeload.github.com/")

	h = httplib2.Http(timeout=1)
	resp = h.request(url, 'HEAD')[0]

	etag = None
	if "etag" in resp:
		etag = resp["etag"].strip('"')

	last_modified = None
	if "last-modified" in resp:
		last_modified = datetime.strptime(resp["last-modified"], "%a, %d %b %Y %H:%M:%S %Z")

	return etag, last_modified

# This queries AWS, looks at every file, if we already knew about that file and the
# last_modified date is != our cached last_modified date, assume we know what we're
# talking about.  Otherwise, redownload the file and store its metadata.
def rebuild_cache():
	global aws_cache
	print "Rebuilding cache..."

	# Login to S3
	conn = boto.connect_s3()
	bucket = conn.get_bucket("juliacache")

	# This is the new dictionary we'll use to build up our cache
	new_aws_cache = {}
	sorted_keys = [key for key in bucket.list()]
	sorted_keys.sort(key=lambda x: x.name)

	# Iterate over all the non-etag files
	for k in [k for k in sorted_keys if k.name[-5:] != ".etag"]:
		# First, we must undo any '+'' symbol madness that has gone on.  Note that a proper HTTP
		# URL should never have a ' ' in it naturally; they should all be escaped as '%20', so
		# these will all be former '+' characters, and it's safe to do a replace like this
		k.name = k.name.replace(' ', '+')

		# If we've never seen this guy before, initialize him, otherwise, copy from aws_cache
		if not k.name in aws_cache:
			new_aws_cache[k.name] = {}
		else:
			new_aws_cache[k.name] = aws_cache[k.name]

		# If we've never seen this filename before, (e.g. we just initialized it above)
		# OR the file has been modified since we last looked in, then download metadata for this file
		if not "modified" in new_aws_cache[k.name] or new_aws_cache[k.name]["modified"] != k.last_modified:
			# Remember that the etag that comes from S3 is _not_ the same as the .etag we store!
			new_aws_cache[k.name]["MD5"] = k.etag
			new_aws_cache[k.name]["modified"] = boto.utils.parse_ts(k.last_modified)
			new_aws_cache[k.name]["size"] = k.size

		# If we have an etag for this file, then load it in:
		etag_keys = [z for z in sorted_keys if z.name == (k.name + ".etag")]
		if len(etag_keys):
			etag = etag_keys[0].get_contents_as_string()
			new_aws_cache[k.name]["etag"] = etag
			print "Loaded %s with etag: %s"%(k.name, etag)
		else:
			print "Loaded %s"%(k.name)

	print "Done rebuilding, with %d cached files"%(len(new_aws_cache))
	# Finally, move new_aws_cache over to aws_cache, effectively clearing out old stuff
	aws_cache = new_aws_cache


def check_consistency(url, name):
	global aws_cache

	# If we already have the file, we can quickly double-check that the
	# file we have cached is still consistent by checking ETag/Last-Modified times
	try:
		etag, last_modified = probe_etag_and_modified(url)
	except:
		# If we run into an error during probe_etag_and_modified(), we serve our
		# cached file to continue serving while the source server is offline, etc...
		print "[%s] Error while trying to check consistency, serving cached file"%(name)
		traceback.print_exc()
		return True

	# Do we have a stored ETag?
	if "etag" in aws_cache[name]:
		if etag is None:
			# We have a stored etag, but we didn't get one from the server.  Suspicious.  Move on to last-modified.
			print "[%s] ETag unavailable despite previous ETag record, continuing to Last-Modified"%(name)
		else:
			if etag != aws_cache[name]["etag"]:
				# We have a stored etag, and we got one from the server, but they didn't match.  Ring the alarm bells.
				print "[%s] ETag changed! Old: %s, New: %s"%(name, aws_cache[name]["etag"], current_etag)
				return False

			# We have a stored etag, and we got one from the server, and they matched.  That's good enough for us.
			print "[%s] Successfully validated ETag"%(name)
			return True

	# Do we have a last-modified date stored?
	if "modified" in aws_cache[name]:
		if last_modified is None:
			print "[%s] Last-Modified unavailable, serving cached file"%(name)
			return True
		else:
			if last_modified > aws_cache[name]["modified"]:
				print "[%s] Last-Modified out of date! Old: %s, New: %s"%(name, str(aws_cache[name]["modified"]), str(last_modified))
				return False
			else:
				print "[%s] Successfully validated Last-Modified"%(name)
				return True

	# By default, just serve the cached stuff
	return True


# Asking for a full URL after <path:url> queries the cache
@app.route("/<path:url>")
def cache(url):
	global aws_cache

	# If this is a sourceforge url, and we're asking for something that ends in /download, get
	# rid of it; it's not necessary, and we can roll without it.  We also don't mind redirecting
	# users to URLs without /download, even if we don't cache it at all.
	if "sourceforge" in url and url[-9:] == "/download":
		url = url[:-9]

	if any([re.match(black_url, url) for black_url in blacklist]):
		print "404'ing %s because it's on the blacklist"%(url)
		abort(404)

	# If it's on the greylist, just forward them on right now
	if any([re.match(grey_url, url) for grey_url in greylist]):
		print "301'ing %s to canonical URL because it's on the greylist"%(url)
		return redirect(url, code=301)

	# Ensure this URL is something we want to touch and if it's not, send them on their merry way
	if not any([re.match(white_url, url) for white_url in whitelist]):
		print "301'ing %s to canonical URL because it's not on the whitelist"%(url)
		return redirect(url, code=301)

	if "github" in url and (basename(dirname(url)) in ["archive", "tarball"]):
		name = basename(dirname(dirname(url))) + "-" + basename(url)
	else:
		name = basename(url)


	# Search for `name` in the cache already.  If it's not there, we need to upload it.
	if not name in aws_cache:
		# Start a thread downloading, but return immediately pointing the user to the original URL
		thread.start_new_thread(add_to_cache, (url,name))
		return redirect(url, code=302)

	# If we fail our consistency check, then redownload the file
	if not check_consistency(url, name):
		remove_from_cache(name)
		thread.start_new_thread(add_to_cache, (url,name))
		return redirect(url, code=302)

	# Now forward them onto the proxy, permanently.
	return redirect("https://juliacache.s3.amazonaws.com/"+name, code=301)

# Fancyness!  Adapted from http://goo.gl/FrdBC0
def sizefmt(num, suffix='B'):
	for unit in ['','K','M','G','T','P','E','Z']:
		if abs(num) < 1024.0:
			return "%3.1f%s%s" % (num, unit, suffix)
		num /= 1024.0
	return "%.1f%s%s" % (num, 'Y', suffix)





# First thing we do is rebuild the cache:
rebuild_cache()

# Asking for nothing gives you the currently cached files
@app.route("/")
def index():
	html  = "<html>Currently caching <b>%d</b> files:<br/><br/>\n"%(len(aws_cache))
	html += "<table style=\"font-family: monospace;\">"
	sorted_keys = aws_cache.keys()
	sorted_keys.sort()
	for k in sorted_keys:
		html += "<tr>"
		html += "<td><b>%s</b></td>"%(k)
		html += "<td style=\"padding-right: 15px;\">MD5: <b>%s</b></td>"%(aws_cache[k]["MD5"])
		html += "<td style=\"padding-right: 15px;\">Modified: <b>%s</b></td>\n"%(str(aws_cache[k]["modified"]))
		html += "<td style=\"padding-right: 15px;\">Size: <b>%s</b></td>\n"%(sizefmt(aws_cache[k]["size"]))
		html += "<td>"
		if "etag" in aws_cache[k]:
			html += "ETag: <b>%s</b>"%(aws_cache[k]["etag"])
		html += "</td>"
		html += "</tr>"

	html += "</table>"
	return html + "</html>"

if __name__ == "__main__":
	app.run(threaded=True)
