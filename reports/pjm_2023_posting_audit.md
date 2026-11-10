# PJM day-ahead posting evidence for the 2023 validation events

The public hourly DA export includes delivery hour, LMP, and version number.
Its schema does not include a publication timestamp. PJM's timestamped [DA
posting feed](https://api.pjm.com/syndication/v1/da_hrl_lmps/rss) reports the
market date in each notice. The snapshot retrieved on November 8, 2026
contained 100 notices from August 1 through November 8, 2026. It did not cover
2023. A posting notice can establish that prices were available by the
notice timestamp. Revision evidence must still identify the applicable version.

PJM's [2023 market rules](https://www.pjm.com/-/media/DotCom/documents/manuals/archive/m11/m11v126-energy-and-ancillary-services-market-operations-05-31-2023.pdf)
give 13:30 Eastern as the scheduled deadline. Its [Markets Gateway guide](https://www.pjm.com/-/media/DotCom/etools/markets-gateway/markets-gateway-user-guide.ashx)
describes delay notifications. The RSS snapshot contains a notice timestamp
of 13:41 Eastern on August 2, 2026, after that scheduled deadline. The schedule
alone cannot establish a date-specific upper bound. The [Data Miner release
notes](https://www.pjm.com/markets-and-operations/etools/data-miner-2/data-miner-2-release-notes)
describe repost metadata with the dates and affected pricing nodes.

For the six eligible events, these nine delivery dates cover the six full
hours before and after each trip. The request for historical evidence should
specify PJM-RTO node 1, the first publication time with UTC offset, and any
repost time and version for each delivery date.

| NRC candidate | Trip onset (Eastern) | Delivery dates needing evidence |
| --- | --- | --- |
| 2023:1 | January 5 at 12:42 EST | January 5 |
| 2023:24 | August 10 at 00:39 EDT | August 9–10 |
| 2023:25 | August 11 at 04:29 EDT | August 10–11 |
| 2023:36 | November 7 at 16:17 EST | November 7 |
| 2023:37 | November 10 at 01:18 EST | November 9–10 |
| 2023:39 | November 16 at 02:27 EST | November 15–16 |

The nine dates cover event windows only. The benchmark also needs
publication evidence and completed event and disruption review for the
comparison hours. The local audit calendar leaves posting times blank until
there is evidence for the relevant price version.

## Draft request to PJM Data Miner support

**To** — dataminer2support@pjm.com. [PJM Data Miner support contact](https://www.pjm.com/-/media/DotCom/committees-groups/forums/tech-change/2026/20260209/20260209-item-04b---product-roadmap---data-miner.pdf#page=8)

**Subject** — Historical day-ahead LMP posting and repost records for PJM RTO

Hello PJM Data Miner support,

I am using Data Miner for a research study and need to establish when the
day-ahead hourly LMP for PJM RTO (pricing node ID 1, feed `da_hrl_lmps`) first
became available. Could you provide the historical posting and repost
timestamps, or direct me to an archive that contains them?

The priority delivery dates are January 5, August 9–11, and November 7, 9, 10,
15, and 16, all in 2023. For each date, could you provide the first
publication timestamp with a UTC offset, the corresponding
`version_nbr`, and any later repost timestamps, versions, and affected
delivery hours for node 1? Please indicate whether each time refers to
publication in Data Miner, Markets Gateway, or another system. An archived
notification that establishes when the applicable version was already
available would also help.

For the comparison hours, could you provide the same records for all
delivery dates from January 1, 2018 through December 31, 2025, preferably
as a machine-readable file? If you can provide only a subset, a complete
2023 extract would help us check the validation period first. If the
timestamps are unavailable, can PJM verify a latest publication time for
the original version on individual dates and identify any exceptions or
reposts?

Thank you,

Varun Fuloria
