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

dl "https://www.aia.com/content/dam/group/en/docs/press-release/2021/AIA%20Interim%20Report%202021%20%28Eng%29.pdf.coredownload.inline.pdf" "AIA_Interim_Report_1H2021.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2022/AIA%20Interim%20Report%202022_E.pdf" "AIA_Interim_Report_1H2022.pdf"
dl "https://www1.hkexnews.hk/listedco/listconews/sehk/2023/0920/2023092000215.pdf" "AIA_Interim_Report_1H2023.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2024/2024%20Interim%20Report%20%28Eng%29.pdf" "AIA_Interim_Report_1H2024.pdf"
dl "https://www.aia.com/content/dam/group-wise/en/docs/investor-relations/2025/e_2025%20Interim%20Report%20%28ESS%29.pdf" "AIA_Interim_Report_1H2025.pdf"
