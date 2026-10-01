#!/bin/bash
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# Write next to this script, so the PDFs always land in data/aia_reports/
# regardless of where the script is invoked from.
cd "$(dirname "$0")" || exit 1

dl () {
  url="$1"; out="$2"
  echo "--> $out"
  curl -sSL --fail --retry 3 --retry-delay 2 -A "$UA" -o "$out" "$url" \
    && echo "    OK $(du -h "$out" | cut -f1)" \
    || echo "    FAILED $url"
}

dl "https://www.aia.com/content/dam/group/en/docs/annual-report/Annual%20Report%202021_E.pdf.coredownload.inline.pdf" "AIA_Annual_Report_FY2021.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/annual-report/Annual_Report_2022_ENG.pdf" "AIA_Annual_Report_FY2022.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2024/2024041200326%201.pdf" "AIA_Annual_Report_FY2023.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/2024%20Annual%20Report%20%28Eng%29.pdf" "AIA_Annual_Report_FY2024.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2026/2025%20Annual%20Report%20%28Eng%29.pdf" "AIA_Annual_Report_FY2025.pdf"
