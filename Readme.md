# Overlay Number Formats in the US

In some areas in the US carriers require that called party numbers sent to the PSTN by an enterprise need to
differentiate between different destination types. For example in NPA 816 these number formats are required:

* HNPA local: 7D
* FNPA local: 10D
* HNPA toll: 1+10D
* FNPA toll: 10+10D

Here HNPA and FNPA stand for home (same NPA as caller) and foreign (different NPA than caller) NPA.

With this Python script for a given NPA/NXX the required called party transformation patterns or route patterns can be
provisioned in Cisco UCM to make sure that +E.164 called party information is properly transformed to the required
number format.
The information needed to determine the transformations is obtained from localcallingguide.com