#!/usr/bin/env python3

import argparse
import os
import errno
import sys
import shutil
from distutils.dir_util import copy_tree
import tempfile
from hashlib import sha1
import glob
import subprocess
from subprocess import Popen, PIPE, STDOUT, DEVNULL
import regex
import se.formatting
import se.easy_xml
import se.epub


IGNORED_FILES = ["colophon.xhtml", "titlepage.xhtml", "imprint.xhtml", "uncopyright.xhtml", "halftitle.xhtml", "toc.xhtml", "loi.xhtml"]
XHTML_NAMESPACES = {"xhtml": "http://www.w3.org/1999/xhtml", "epub": "http://www.idpf.org/2007/ops", "z3998": "http://www.daisy.org/z3998/2012/vocab/structure/", "se": "https://standardebooks.org/vocab/1.0", "dc": "http://purl.org/dc/elements/1.1/"}

#Some convenience aliases
WORD_JOINER = "\u2060"		#word joiner, U+2060
HAIR_SPACE = "\u200a"		#hair space, U+200A
ZERO_WIDTH_SPACE = "\ufeff"	#zero-width no-break space, U+FEFF
SHY_HYPHEN = "\u00ad"		#soft hyphen, U+00AD

COVER_SVG_WIDTH = 1400
COVER_SVG_HEIGHT = 2100
COVER_THUMBNAIL_WIDTH = COVER_SVG_WIDTH / 4
COVER_THUMBNAIL_HEIGHT = COVER_SVG_HEIGHT / 4


def main():
	parser = argparse.ArgumentParser(description="Build compatible .epub and pure .epub3 ebooks from a Standard Ebook source directory.  Output is placed in the current directory, or the target directory with --output-dir.")
	parser.add_argument("-v", "--verbose", action="store_true", help="increase output verbosity")
	parser.add_argument("-o", "--output-dir", dest="output_directory", metavar="DIRECTORY", type=str, help="a directory to place output files in; will be created if it doesn't exist")
	parser.add_argument("-c", "--check", action="store_true", help="use epubcheck to validate the compatible .epub file; if --kindle is also specified and epubcheck fails, don't create a Kindle file")
	parser.add_argument("-k", "--kindle", dest="build_kindle", action="store_true", help="also build an .azw3 file for Kindle")
	parser.add_argument("-b", "--kobo", dest="build_kobo", action="store_true", help="also build a .kepub.epub file for Kobo")
	parser.add_argument("-t", "--covers", dest="build_covers", action="store_true", help="output the cover and a cover thumbnail")
	parser.add_argument("-p", "--proof", action="store_true", help="insert additional CSS rules that are helpful for proofreading; output filenames will end in .proof")
	parser.add_argument("source_directory", metavar="DIRECTORY", help="a Standard Ebooks source directory")
	args = parser.parse_args()

	script_directory = os.path.dirname(os.path.realpath(__file__))
	epubcheck_path = shutil.which("epubcheck")
	ebook_convert_path = shutil.which("ebook-convert")
	mogrify_path = shutil.which("mogrify")
	rsvg_convert_path = shutil.which("rsvg-convert")
	convert_path = shutil.which("convert")
	simplify_tags_path = os.path.join(script_directory, "simplify-tags")
	clean_path = os.path.join(script_directory, "clean")
	hyphenate_path = os.path.join(script_directory, "hyphenate")
	toc2kindle_path = os.path.join(script_directory, "toc2kindle")
	endnotes2kindle_path = os.path.join(script_directory, "endnotes2kindle")
	update_asin_path = os.path.join(script_directory, "update-asin")
	xsl_filename = os.path.join(script_directory, "data", "navdoc2ncx.xsl")

	# Check for some required tools
	if args.check and epubcheck_path is None:
		print("Error: Couldn't locate epubcheck. Is it installed?", file=sys.stderr)
		exit(1)

	if mogrify_path is None:
		print("Error: Couldn't locate mogrify. Is Imagemagick installed?", file=sys.stderr)
		exit(1)

	if rsvg_convert_path is None:
		print("Error: Couldn't locate rsvg-convert. Is librsvg2-bin installed?", file=sys.stderr)
		exit(1)

	if args.build_kindle and ebook_convert_path is None:
		print("Error: Couldn't locate ebook-convert. Is Calibre installed?", file=sys.stderr)
		exit(1)

	if args.build_kindle and convert_path is None:
		print("Error: Couldn't locate convert. Is Imagemagick installed?", file=sys.stderr)
		exit(1)

	# Check the output directory and create it if it doesn't exist
	if args.output_directory is None:
		output_directory = os.getcwd()
	else:
		output_directory = args.output_directory

	output_directory = os.path.abspath(output_directory)

	if os.path.exists(output_directory):
		if not os.path.isdir(output_directory):
			print("Error: Not a directory: {}".format(output_directory), file=sys.stderr)
			exit(1)
	else:
		# Doesn't exist, try to create it
		try:
			os.makedirs(output_directory)
		except OSError as exception:
			if exception.errno != errno.EEXIST:
				print("Error: Couldn't create output directory", file=sys.stderr)
				exit(1)

	# Confirm source directory exists and is an SE source directory
	if not os.path.exists(args.source_directory) or not os.path.isdir(args.source_directory):
		print("Error: Not a directory: {}".format(args.source_directory), file=sys.stderr)
		exit(1)

	source_directory = os.path.abspath(args.source_directory)

	if not os.path.isdir(os.path.join(source_directory, "src")):
		print("Error: Doesn't look like a Standard Ebooks source directory: {}".format(source_directory), file=sys.stderr)
		exit(1)

	# All clear to start building!
	if args.verbose:
		print("Building {} ...".format(source_directory))

	with tempfile.TemporaryDirectory() as work_directory:
		work_epub_root_directory = os.path.join(work_directory, "src")

		copy_tree(source_directory, work_directory)
		shutil.rmtree(os.path.join(work_directory, ".git"))

		with open(os.path.join(work_epub_root_directory, "epub", "content.opf"), "r") as file:
			metadata_xhtml = file.read()
			metadata_tree = se.easy_xml.EasyXmlTree(metadata_xhtml)

		title = metadata_tree.xpath("//dc:title")[0].inner_html()
		url_title = se.formatting.make_url_safe(title)

		author = metadata_tree.xpath("//dc:creator")[0].inner_html()
		url_author = se.formatting.make_url_safe(author)

		epub_output_filename = "{}_{}{}.epub".format(url_title, url_author, ".proof" if args.proof else "")
		epub3_output_filename = "{}_{}{}.epub3".format(url_title, url_author, ".proof" if args.proof else "")
		kobo_output_filename = "{}_{}{}.kepub.epub".format(url_title, url_author, ".proof" if args.proof else "")
		kindle_output_filename = "{}_{}{}.azw3".format(url_title, url_author, ".proof" if args.proof else "")

		# Clean up old output files if any
		for kindle_thumbnail in glob.glob(os.path.join(output_directory, "thumbnail_*_EBOK_portrait.jpg")):
			se.epub.quiet_remove(kindle_thumbnail)
		se.epub.quiet_remove(os.path.join(output_directory, "cover.jpg"))
		se.epub.quiet_remove(os.path.join(output_directory, "cover-thumbnail.jpg"))
		se.epub.quiet_remove(os.path.join(output_directory, epub_output_filename))
		se.epub.quiet_remove(os.path.join(output_directory, epub3_output_filename))
		se.epub.quiet_remove(os.path.join(output_directory, kobo_output_filename))
		se.epub.quiet_remove(os.path.join(output_directory, kindle_output_filename))

		# Are we including proofreading CSS?
		if args.proof:
			with open(os.path.join(work_epub_root_directory, "epub", "css", "local.css"), "a", encoding="utf-8") as local_css_file:
				with open(os.path.join(script_directory, "templates", "proofreading.css"), "r", encoding="utf-8") as proofreading_css_file:
					local_css_file.write(proofreading_css_file.read())

		# Output the pure epub3 file
		if args.verbose:
			print("\tBuilding {} ...".format(epub3_output_filename), end="", flush=True)

		se.epub.write_epub(os.path.join(output_directory, epub3_output_filename), work_epub_root_directory)

		if args.verbose:
			print(" OK")

		if args.build_kobo:
			if args.verbose:
				print("\tBuilding {} ...".format(kobo_output_filename), end="", flush=True)
		else:
			if args.verbose:
				print("\tBuilding {} ...".format(epub_output_filename), end="", flush=True)

		# Now add epub2 compatibility.

		# Tell xmllint to indent with tabs using an environmental variable
		env = os.environ.copy()
		env["XMLLINT_INDENT"] = "\t"

		# Include compatibility CSS
		with open(os.path.join(work_epub_root_directory, "epub", "css", "core.css"), "a", encoding="utf-8") as core_css_file:
			with open(os.path.join(script_directory, "templates", "compatibility.css"), "r", encoding="utf-8") as compatibility_css_file:
				core_css_file.write(compatibility_css_file.read())

		# Simplify tags
		output = subprocess.check_output([simplify_tags_path, work_epub_root_directory]).decode().strip()
		if output:
			print("{}Error: simplify-tags failed with: ".format("\t" if args.verbose else ""), file=sys.stderr)
			exit(1)

		# Extract cover and cover thumbnail
		# Mogrify reports the svg as the wrong size, so we have to force the size.
		# Note that we can't use percentages to resize, since mogrify auto-detects the wrong svg size to begin with.
		subprocess.check_output(["mogrify", "-resize", "{}x{}".format(COVER_SVG_WIDTH, COVER_SVG_HEIGHT), "-format", "jpg", os.path.join(work_epub_root_directory, "epub", "images", "cover.svg")])

		if args.build_covers:
			shutil.copy2(os.path.join(work_epub_root_directory, "epub", "images", "cover.jpg"), os.path.join(output_directory, "cover.jpg"))
			shutil.copy2(os.path.join(work_epub_root_directory, "epub", "images", "cover.svg"), os.path.join(output_directory, "cover-thumbnail.svg"))
			subprocess.check_output(["mogrify", "-resize", "{}x{}".format(COVER_THUMBNAIL_WIDTH, COVER_THUMBNAIL_HEIGHT), "-quality", "100", "-format", "jpg", os.path.join(output_directory, "cover-thumbnail.svg")])
			os.remove(os.path.join(output_directory, "cover-thumbnail.svg"))

		os.remove(os.path.join(work_epub_root_directory, "epub", "images", "cover.svg"))

		# Massage image references in content.opf
		metadata_xhtml = metadata_xhtml.replace("cover.svg", "cover.jpg")
		metadata_xhtml = metadata_xhtml.replace(".svg", ".png")
		metadata_xhtml = metadata_xhtml.replace("id=\"cover.jpg\" media-type=\"image/svg+xml\"", "id=\"cover.jpg\" media-type=\"image/jpeg\"")
		metadata_xhtml = metadata_xhtml.replace("image/svg+xml", "image/png")
		metadata_xhtml = metadata_xhtml.replace("properties=\"svg\"", "")

		# Output the modified content.opf so that we can build the kobo book before making more epub2 compatibility hacks
		with open(os.path.join(work_epub_root_directory, "epub", "content.opf"), "w") as file:
			file.write(metadata_xhtml)
			file.truncate()

		# Recurse over xhtml files to make some compatibility replacements
		for root, _, filenames in os.walk(work_epub_root_directory):
			for filename in filenames:
				if filename.lower().endswith(".svg"):
					# Convert SVGs to PNGs
					subprocess.check_output(["rsvg-convert", "-z", "2", "-a", "-f", "png", "-o", regex.sub(r"\.svg$", r".png", os.path.join(root, filename)), os.path.join(root, filename)])
					os.remove(os.path.join(root, filename))

				if filename.lower().endswith(".xhtml"):
					with open(os.path.join(root, filename), "r+", encoding="utf-8") as file:
						xhtml = file.read()
						processed_xhtml = xhtml

						# Google Play Books chokes on https XML namespace identifiers (as of at least 2017-07)
						processed_xhtml = processed_xhtml.replace("https://standardebooks.org/vocab/1.0", "http://standardebooks.org/vocab/1.0")

						# We converted svgs to pngs, so replace references
						processed_xhtml = processed_xhtml.replace("cover.svg", "cover.jpg")
						processed_xhtml = processed_xhtml.replace(".svg", ".png")

						#To get popup footnotes in iBooks, we have to change epub:rearnote to epub:footnote.
						#Remember to get our custom style selectors too.
						processed_xhtml = regex.sub(r"epub:type=\"([^\"]*?)rearnote([^\"]*?)\"", "epub:type=\"\\1footnote\\2\"", processed_xhtml)
						processed_xhtml = regex.sub(r"class=\"([^\"]*?)epub-type-rearnote([^\"]*?)\"", "class=\"\\1epub-type-footnote\\2\"", processed_xhtml)

						#Include extra lang tag for accessibility compatibility.
						processed_xhtml = regex.sub(r"xml\:lang\=\"([^\"]+?)\"", "lang=\"\\1\" xml:lang=\"\\1\"", processed_xhtml)

						# Typography: replace double and triple em dash characters with extra em dashes.
						processed_xhtml = processed_xhtml.replace("⸺", "—{}—".format(WORD_JOINER))
						processed_xhtml = processed_xhtml.replace("⸻", "—{}—{}—".format(WORD_JOINER, WORD_JOINER))

						# Typography: replace some other less common characters.
						processed_xhtml = processed_xhtml.replace("⅒", "1/10")
						processed_xhtml = processed_xhtml.replace("℅", "c/o")

						# Many e-readers don't support the word joiner character (U+2060).
						# They DO, however, support the now-deprecated zero-width non-breaking space (U+FEFF)
						# For epubs, do this replacement.  Kindle now seems to handle everything fortunately.
						processed_xhtml = processed_xhtml.replace(WORD_JOINER, ZERO_WIDTH_SPACE)

						if processed_xhtml != xhtml:
							file.seek(0)
							file.write(processed_xhtml)
							file.truncate()

				if filename.lower().endswith(".css"):
					with open(os.path.join(root, filename), "r+", encoding="utf-8") as file:
						css = file.read()
						processed_css = css

						#To get popup footnotes in iBooks, we have to change epub:rearnote to epub:footnote.
						#Remember to get our custom style selectors too.
						processed_css = processed_css.replace("rearnote", "footnote")

						if processed_css != css:
							file.seek(0)
							file.write(processed_css)
							file.truncate()

		if args.build_kobo:
			with tempfile.TemporaryDirectory() as kobo_work_directory:
				copy_tree(work_epub_root_directory, kobo_work_directory)

				se.epub.write_epub(os.path.join(output_directory, kobo_output_filename), kobo_work_directory)

			if args.verbose:
				print(" OK")
				print("\tBuilding {} ...".format(kobo_output_filename), end="", flush=True)

		# Now work on more epub2 compatibility

		# Include epub2 cover metadata
		cover_id = metadata_tree.xpath("//opf:item[@properties=\"cover-image\"]/@id")[0]
		metadata_xhtml = regex.sub(r"(<metadata[^>]+?>)", "\\1\n\t\t<meta content=\"{}\" name=\"cover\" />".format(cover_id), metadata_xhtml)

		# Add metadata to content.opf indicating this file is a Standard Ebooks compatibility build
		metadata_xhtml = metadata_xhtml.replace("<dc:publisher", "<meta property=\"se:transform\">compatibility</meta>\n\t\t<dc:publisher")

		# Generate our NCX file for epub2 compatibility.
		# First find the ToC file.
		toc_filename = metadata_tree.xpath("//opf:item[@properties=\"nav\"]/@href")[0]
		metadata_xhtml = metadata_xhtml.replace("<spine>", "<spine toc=\"ncx\">")
		metadata_xhtml = metadata_xhtml.replace("<manifest>", "<manifest><item href=\"toc.ncx\" id=\"ncx\" media-type=\"application/x-dtbncx+xml\" />")

		# Now use an XSLT transform to generate the NCX
		toc_tree = se.epub.convert_toc_to_ncx(work_epub_root_directory, toc_filename, xsl_filename)

		# Convert the <nav> landmarks element to the <guide> element in content.opf
		guide_xhtml = "<guide>"
		for element in toc_tree.xpath("//xhtml:nav[@epub:type=\"landmarks\"]/xhtml:ol/xhtml:li/xhtml:a"):
			element_xhtml = element.tostring()
			element_xhtml = regex.sub(r"epub:type=\"([^\"]*)(\s*frontmatter\s*|\s*backmatter\s*)([^\"]*)\"", "type=\"\\1\\3\"", element_xhtml)
			element_xhtml = regex.sub(r"epub:type=\"[^\"]*(acknowledgements|bibliography|colophon|copyright-page|cover|dedication|epigraph|foreword|glossary|index|loi|lot|notes|preface|bodymatter|titlepage|toc)[^\"]*\"", "type=\"\\1\"", element_xhtml)
			element_xhtml = element_xhtml.replace("type=\"copyright-page", "type=\"copyright page")

			# We add the 'text' attribute to the titlepage to tell the reader to start there
			element_xhtml = element_xhtml.replace("type=\"titlepage", "type=\"title-page text")

			element_xhtml = element_xhtml.replace("type=\"appendix", "type=\"")
			element_xhtml = regex.sub(r"type=\"\s*\"", "", element_xhtml)
			element_xhtml = element_xhtml.replace("<a", "<reference")
			element_xhtml = regex.sub(r">(.+)</a>", " title=\"\\1\" />", element_xhtml)

			guide_xhtml = guide_xhtml + element_xhtml

		guide_xhtml = guide_xhtml + "</guide>"

		metadata_xhtml = metadata_xhtml.replace("</package>", "") + guide_xhtml + "</package>"

		# Guide is done, now write content.opf and clean it
		# Output the modified content.opf so that we can build the kobo book before making more epub2 compatibility hacks
		with open(os.path.join(work_epub_root_directory, "epub", "content.opf"), "w") as file:
			file.write(metadata_xhtml)
			file.truncate()

		# Recurse over css files to make some compatibility replacements
		for root, _, filenames in os.walk(work_epub_root_directory):
			for filename in filenames:
				if filename.lower().endswith(".css"):
					with open(os.path.join(root, filename), "r+", encoding="utf-8") as file:
						css = file.read()
						processed_css = css

						processed_css = regex.sub(r"(page\-break\-(before|after|inside)\s*\:\s*(.+))", "\\1\n\t-webkit-column-break-\\2: \\3 /* For Readium */", processed_css)
						processed_css = regex.sub(r"^\s*hyphens\s*\:\s*(.+)", "\thyphens: \\1\n\tadobe-hyphenate: \\1\n\t-webkit-hyphens: \\1\n\t-epub-hyphens: \\1\n\t-moz-hyphens: \\1", processed_css)
						processed_css = regex.sub(r"^\s*hyphens\s*\:\s*none;", "\thyphens: none;\n\tadobe-text-layout: optimizeSpeed; /* For Nook */", processed_css)

						if processed_css != css:
							file.seek(0)
							file.write(processed_css)
							file.truncate()

		# Add soft hyphens
		output = subprocess.check_output([hyphenate_path, "--ignore-h-tags", work_epub_root_directory]).decode().strip()

		# All done, clean the output
		output = subprocess.check_output([clean_path, work_epub_root_directory]).decode().strip()

		# Write the compatible epub
		se.epub.write_epub(os.path.join(output_directory, epub_output_filename), work_epub_root_directory)

		if args.verbose:
			print(" OK")

		if args.check:
			if args.verbose:
				print("\tRunning epubcheck on {} ...".format(epub_output_filename), end="", flush=True)

			process = Popen([epubcheck_path, "--quiet", os.path.join(output_directory, epub_output_filename)], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
			output = process.stdout.read().decode().strip()

			if output:
				if args.verbose:
					print("\n\t\t" + "\t\t".join(output.splitlines(True)), file=sys.stderr)
				else:
					print(output, file=sys.stderr)

				exit(1)

			if args.verbose:
				print(" OK")


		if args.build_kindle:
			if args.verbose:
				print("\tBuilding {} ...".format(kindle_output_filename), end="", flush=True)


			# Kindle doesn't go more than 2 levels deep for ToC, so flatten it here.
			output = subprocess.check_output([toc2kindle_path, os.path.join(work_epub_root_directory, "epub", toc_filename)]).decode().strip()

			# Rebuild the NCX
			toc_tree = se.epub.convert_toc_to_ncx(work_epub_root_directory, toc_filename, xsl_filename)

			# Clean just the ToC and NCX
			output = subprocess.check_output([clean_path, os.path.join(work_epub_root_directory, "epub", "toc.ncx"), os.path.join(work_epub_root_directory, "epub", toc_filename)]).decode().strip()

			# Do some compatibility replacements
			for root, _, filenames in os.walk(work_epub_root_directory):
				for filename in filenames:
					if filename.lower().endswith(".xhtml"):
						with open(os.path.join(root, filename), "r+", encoding="utf-8") as file:
							xhtml = file.read()
							processed_xhtml = xhtml

							# Kindle doesn't recognize most zero-width spaces or word joiners, so just remove them.
							# It does recognize the word joiner character, but only in the old mobi7 format.  The new format renders them as spaces.
							processed_xhtml = processed_xhtml.replace(ZERO_WIDTH_SPACE, "")

							# Remove the epub:type attribute, as Calibre turns it into just "type"
							processed_xhtml = regex.sub(r"epub:type=\"[^\"]*?\"", "", processed_xhtml)

							if processed_xhtml != xhtml:
								file.seek(0)
								file.write(processed_xhtml)
								file.truncate()

			# Include compatibility CSS
			with open(os.path.join(work_epub_root_directory, "epub", "css", "core.css"), "a", encoding="utf-8") as core_css_file:
				with open(os.path.join(script_directory, "templates", "kindle.css"), "r", encoding="utf-8") as compatibility_css_file:
					core_css_file.write(compatibility_css_file.read())

			# Convert endnotes to Kindle popup compatible notes
			if os.path.isfile(os.path.join(work_epub_root_directory, "epub", "text", "endnotes.xhtml")):
				output = subprocess.check_output([endnotes2kindle_path, os.path.join(work_epub_root_directory, "epub", "text", "endnotes.xhtml")]).decode().strip()

				# While Kindle now supports soft hyphens, popup endnotes break words but don't insert the hyphen characters.  So for now, remove soft hyphens from the endnotes file.
				with open(os.path.join(work_epub_root_directory, "epub", "text", "endnotes.xhtml"), "r+", encoding="utf-8") as core_css_file:
					xhtml = file.read()
					processed_xhtml = xhtml

					processed_xhtml = processed_xhtml.replace(SHY_HYPHEN, "")

					if processed_xhtml != xhtml:
						file.seek(0)
						file.write(processed_xhtml)
						file.truncate()

			# Build an epub file we can send to Calibre
			se.epub.write_epub(os.path.join(work_directory, epub_output_filename), work_epub_root_directory)

			# Generate the kindle file
			# We place it in the work directory because later we have to update the asin, and the `update-asin` script will write to the final output directory
			cover_path = os.path.join(work_epub_root_directory, "epub", metadata_tree.xpath("//opf:item[@properties=\"cover-image\"]/@href")[0].replace(".svg", ".jpg"))
			return_code = subprocess.call([ebook_convert_path, os.path.join(work_directory, epub_output_filename), os.path.join(work_directory, kindle_output_filename), "--pretty-print", "--no-inline-toc", "--max-toc-links=0", "--prefer-metadata-cover", "--cover={}".format(cover_path)], stdout=DEVNULL, stderr=DEVNULL)

			if return_code:
				print("{}Error: ebook-convert failed".format("\t" if args.verbose else ""), file=sys.stderr)
				exit(1)
			else:
				# Success, extract the Kindle cover thumbnail
				# By convention the ASIN is set to the SHA-1 sum of the book's identifying URL
				identifier = metadata_tree.xpath("//dc:identifier")[0].inner_html().replace("url:", "")
				asin = sha1(identifier.encode("utf-8")).hexdigest()

				# Update the ASIN in the generated file
				subprocess.call([update_asin_path, asin, os.path.join(work_directory, kindle_output_filename), os.path.join(output_directory, kindle_output_filename)], stdout=DEVNULL, stderr=DEVNULL)

				#Extract the thumbnail
				subprocess.call([convert_path, os.path.join(work_epub_root_directory, "epub", "images", "cover.jpg"), "-resize", "432x660", os.path.join(output_directory, "thumbnail_{}_EBOK_portrait.jpg".format(asin))], stdout=DEVNULL, stderr=DEVNULL)

			if args.verbose:
				print(" OK")


if __name__ == "__main__":
	main()
