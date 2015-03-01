#!/usr/bin/env python
from flask import Flask, redirect
import os, urllib, thread, boto, re
from os.path import dirname, basename
from boto.s3.key import Key
app = Flask(__name__)

# Login to S3
conn = boto.connect_s3()
bucket = conn.get_bucket("juliacache")

# This is the list of files we have successfully cached in the past and can spit out immediately
aws_cache = [z.name for z in bucket.get_all_keys()]

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
	"gmplib.org/download/gmp",
	"mpfr.org/mpfr-current",
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


	# You're naughty, so you get to sit in the corner, away from the other URLs
	"sourceforge.net/projects/pcre/files/pcre/[^/]+/[^/]+/download",
	"downloads.sourceforge.net/sevenzip",
]

# Take a stripped-down URL and add all the regex stuff to make it something we'd have dinner with
def regexify(url):
	# I hate sourceforge a little more every day
	if not "sourceforge" in url:
		return r"https?://(www\.)?" + url.replace(r".", r"\.") + r"/[^/]+$"
	else:
		return r"https?://(www\.)?" + url.replace(r".", r"\.")
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
		if os.stat(tmp_name).st_size == 0:
			pending_cache.remove(name)
			return

		print "[%s] Finished download: %s (%d bytes)"%(name, tmp_name, os.stat(tmp_name).st_size)

		# Upload it to AWS and cleanup the temporary file
		print "[%s] Starting upload"%(name)
		k = Key(bucket)
		k.key = name
		k.set_contents_from_filename(tmp_name)
		k.set_acl('public-read')

		print "[%s] Finished upload"%(name)

		# Finally, add this name into our aws_cache, and remove it from pending_cache
		aws_cache += [name]
		pending_cache.remove(name)
	except IOError as e:
		# If we got a 404, clean up
		print "[%s] 404, halting"%(name)
		pending_cache.remove(name)
		



# Asking for a full URL after <path:url> queries the cache
@app.route("/<path:url>")
def cache(url):
	global aws_cache

	# Ensure this URL is something we want to touch and if it's not, send them on their merry way
	if not any([re.match(white_url, url) for white_url in whitelist]):
		print "Rejecting %s because it's not on the list"%(url)
		return redirect(url, code=301)

	# Take basename for storage purposes, dealing with various oddities where we can:
	if "sourceforge" in url:
		# I'M LOOING AT YOU, SOURCEFORGE
		name = basename(dirname(url))
	elif "github" in url and basename(dirname(url)) == "archive":
		name = basename(dirname(dirname(url))) + "-" + basename(url)
	else:
		name = basename(url)

	
	# Search for `name` in the cache already
	if not name in aws_cache:
		# If not, then we need to upload it!  Start a thread working on that, but return immediately pointing
		# the user to the original URL.  We'll have it cached next time (I hope)
		thread.start_new_thread( add_to_cache, (url,name))
		return redirect(url, code=302)

	# Now forward them onto the proxy, permanently
	return redirect("https://juliacache.s3.amazonaws.com/"+name, code=301)


# Asking for nothing gives you the currently cached files
@app.route("/")
def index():
	return "Currently caching <b>%d</b> files:<br/><b>\n%s</b>"%(len(aws_cache), "<br/>\n".join(aws_cache))

if __name__ == "__main__":
	app.run()
