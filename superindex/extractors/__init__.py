"""Document text extraction backends.

`azure_di` uses Azure AI Document Intelligence to turn PDFs into Markdown with
real tables and page anchors. `nav.build` consumes that Markdown directly.
`pdfspine` does the same offline with the optional pdfspine library.
"""
