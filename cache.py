#!/usr/bin/env python
from flask import Flask, redirect
import os, urllib, thread, boto, re, sys, httplib2
from os.path import dirname, basename
app = Flask(__name__)

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

	# Add unicode fonts for libutf8
	"unicode.org/Public/UCD/latest/ucd/auxiliary",
	"unicode.org/Public/UNIDATA",
	"unifoundry.com/pub/unifont-[\d\.]+/font-builds",

	# You're naughty, so you get to sit in the corner, away from the other URLs
	"sourceforge.net/projects/pcre/files/pcre/[^/]+/[^/]+/download",
	"downloads.sourceforge.net/sevenzip",
]

# Take a stripped-down URL and add all the regex stuff to make it something we'd have dinner with
def regexify(url):
	# I hate sourceforge a little more every day
	if url.startswith("sourceforge"):
		return r"^https?://(www\.)?" + url.replace(r".", r"\.")
	else:
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

		# Upload it to AWS and cleanup the temporary file
		print "[%s] Starting upload"%(name)
		# Login to S3
		conn = boto.connect_s3()
		bucket = conn.get_bucket("juliacache")

		k = boto.s3.key.Key(bucket)
		k.key = name
		k.set_contents_from_filename(tmp_name)
		k.set_acl('public-read')
		k.close()

		# Finally, add this name into our aws_cache, and remove it from pending_cache
		k = bucket.get_key(name)

		# Remember that the etag give to us by S3 is _not_ the same as the .etag file we store
		aws_cache[name] = {"MD5":k.etag, "size":filesize, "modified":k.last_modified}

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
	print "Removing %s from cache"%(name)
	del aws_cache[name]

	# Login to S3
	conn = boto.connect_s3()
	bucket = conn.get_bucket("juliacache")
	bucket.delete_key(name)

	# Delete a .etag file on the off chance that it exists (doesn't throw an error)
	bucket.delete_key(name + ".etag")


# Do a HEAD request for the file, getting the etag header
def probe_etag(url):
	# If we're dealing with github, we need to ask codeload to avoid downloading the
	# entire file again.  I think this might be a bug in how python handles redirects
	if not "codeload" in url:
		url.replace("/github.com/", "/codeload.github.com/")

	h = httplib2.Http()
	resp = h.request(url, 'HEAD')[0]
	return resp["etag"].strip('"')


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
	all_keys = bucket.get_all_keys()
	all_keys.sort()
	for k in all_keys:
		# First, check to see if this is an .etag file.  If it is, load in the .etag goodness
		if k.name[-5:] == ".etag":
			etag = k.get_contents_as_string()
			new_aws_cache[k.name[:-5]]["etag"] = etag
			print "Loaded etag for %s: %s"%(k.name[:-5], etag)
		else:
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
				new_aws_cache[k.name]["modified"] = k.last_modified
				new_aws_cache[k.name]["size"] = k.size


	print "Done rebuilding, with %d cached files"%(len(new_aws_cache))
	# Finally, move new_aws_cache over to aws_cache, effectively clearing out old stuff
	aws_cache = new_aws_cache


# Fancyness!  Adapted from http://goo.gl/FrdBC0
def sizefmt(num, suffix='B'):
    for unit in ['','K','M','G','T','P','E','Z']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Y', suffix)

# Asking for a full URL after <path:url> queries the cache
@app.route("/<path:url>")
def cache(url):
	global aws_cache

	# Ensure this URL is something we want to touch and if it's not, send them on their merry way
	if not any([re.match(white_url, url) for white_url in whitelist]):
		print "Rejecting %s because it's not on the list"%(url)
		return redirect(url, code=301)

	# Take basename for storage purposes, dealing with various oddities where we can:
	if "sourceforge" in url and basename(url) == "download":
		# I'M LOOING AT YOU, SOURCEFORGE
		name = basename(dirname(url))
	elif "github" in url and basename(dirname(url)) == "archive":
		name = basename(dirname(dirname(url))) + "-" + basename(url)
	else:
		name = basename(url)

	
	# Search for `name` in the cache already.  If it's not there, we need to upload it.
	if not name in aws_cache:
		# Start a thread downloading, but return immediately pointing the user to the original URL
		thread.start_new_thread(add_to_cache, (url,name))
		return redirect(url, code=302)

	# If we already have the file, but it's a github URL, we can really quickly double-check that the
	# file we have cached is still okay; (This prevents against people moving tags, etc...)
	if "etag" in aws_cache[name]:
		try:
			current_etag = probe_etag(url)
			if current_etag != aws_cache[name]["etag"]:
				print "[%s] ETAG has changed for! Old: %s, New: %s\n"%(name, aws_cache[name]["etag"], current_etag)
				remove_from_cache(name)
				thread.start_new_thread(add_to_cache, (url,name))
				return redirect(url, code=302)

			# Otherwise, let's give a little debugging output to show this is working properly
			print "[%s] Successfully validated ETAG"%(name)
		except:
			print "[%s] Error while trying to validate ETAG, serving cached file"

	# Now forward them onto the proxy, permanently
	return redirect("https://juliacache.s3.amazonaws.com/"+name, code=301)


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
		# boto y u have two different time formats?!
		modified = boto.utils.parse_ts(aws_cache[k]["modified"]).isoformat()
		html += "<td style=\"padding-right: 15px;\">Modified: <b>%s</b></td>\n"%(modified)
		html += "<td style=\"padding-right: 15px;\">Size: <b>%s</b></td>\n"%(sizefmt(aws_cache[k]["size"]))
		html += "<td>"
		if "etag" in aws_cache[k]:
			html += "ETAG: <b>%s</b>"%(aws_cache[k]["etag"])
		html += "</td>"
		html += "</tr>"

	html += "</table>"
	return html + "</html>"

if __name__ == "__main__":
	app.run()
