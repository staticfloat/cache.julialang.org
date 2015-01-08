#!/usr/bin/env python
from flask import Flask, redirect
import os, urllib, thread, subprocess
app = Flask(__name__)


# This is the list of files we have successfully cached in the past and can spit out immediately
aws_cache = subprocess.check_output(["aws", "ls", "-l", "juliacache"]).split()[6::7]

# This is the list of files that are currently downloading, so we don't download it twice
pending_cache = []


def add_to_cache(url, name):
	global pending_cache, aws_cache
	# Stop bad things from happening if we get a flood of requests for a single file
	if name in pending_cache:
		return
	pending_cache += [name]

	print "[%s] Starting download"%(name)

	# Download the requested file
	tmp_name = os.path.join("/tmp", name)
	urllib.urlretrieve(url, tmp_name)

	print "[%s] Finished download"%(name)

	# If nothing was downloaded, just exit out after cleaning up
	if os.stat(tmp_name).st_size == 0:
		os.unlink(tmp_name)
		pending_cache.remove(name)
		return

	# Upload it to AWS and cleanup the temporary file
	print "[%s] Starting upload"%(name)
	os.system("aws put --public juliacache/%s %s"%(name, tmp_name))
	os.unlink(tmp_name)

	print "[%s] Finished upload"%(name)

	# Finally, add this name into our aws_cache, and remove it from pending_cache
	aws_cache += [name]
	pending_cache.remove(name)



# Asking for a full URL after <path:url> queries the cache
@app.route("/<path:name>")
def cache(name):
	global aws_cache
	# Take basename to ensure nobody tries any funny business
	name = os.path.basename(name)
	# Also synthesize full url
	url = "https://download.sf.net/project/machomebrew/Bottles/"+name

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
	return "Currently caching %d files:\n%s"%(len(aws_cache), "\n  ".join(aws_cache))

if __name__ == "__main__":
	app.run(debug=True)