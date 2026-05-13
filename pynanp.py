#!/usr/bin/env python
"""
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
"""

import argparse
import functools
import os
import xml.etree.ElementTree as ET

import requests
import xmljson

import ucmaxl


def xmllocalprefix(npa, nxx):
    """Fetch all NPA-NXX prefixes that are local to a given NPA-NXX from localcallingguide.com.

    Queries the XML API at localcallingguide.com for every NPA-NXX pair in the same local calling
    area as the specified NPA-NXX.

    :param npa: three-digit NPA (area code) of the reference location
    :param nxx: three-digit NXX (central office code) of the reference location
    :return: list of six-digit strings in ``NPANXX`` form for each local prefix
    """
    url = 'https://www.localcallingguide.com/xmllocalprefix.php'
    params = {'npa': npa, 'nxx': nxx}
    response = requests.get(url, params=params)
    data = xmljson.Parker(dict_type=dict).data(ET.fromstring(response.text))
    return ['{npa}{nxx}'.format(npa=prefix['npa'], nxx=prefix['nxx']) for prefix in data['lca-data']['prefix']]


def single_pattern(prefix5d, trailing_digits, home_npa, hnpalocal7d, ios):
    """Build a single dial-plan pattern for a five-digit NPA-NXX prefix and its valid trailing digits.

    Compresses the set of trailing digits into the shortest range notation (e.g. ``[2-5]``, ``23``,
    or ``X`` for 0–9) and combines it with the prefix to produce either a Cisco UCM dial-plan
    pattern or an IOS ``e164-pattern-map`` entry.

    :param prefix5d: first five digits of the NPA-NXX (e.g. ``'81655'``)
    :param trailing_digits: string of digits (0–9) that are valid in the sixth position of the NPA-NXX
    :param home_npa: three-digit home NPA used to identify HNPA destinations
    :param hnpalocal7d: when ``True``, HNPA local patterns use 7D form (NXX + 4 digits) rather than
                        the full 10D form
    :param ios: when ``True``, produce an IOS ``e164-pattern-map`` entry; otherwise produce a UCM pattern
    :return: formatted pattern string ready for provisioning in UCM or an IOS e164-pattern-map
    """
    # Compress the trailing digits into the shortest range notation possible.
    # Examples: '2345' -> '2-5', '23' -> '23', '2' -> '2', '0123456789' -> 'X'
    r = ''
    i = iter(trailing_digits)
    start_digit = next(i)
    done = False
    while not done:
        # Extend the current consecutive run as far as possible
        stop_digit = start_digit
        digit = start_digit
        try:
            while True:
                digit = next(i)
                if int(digit) - int(stop_digit) == 1:
                    stop_digit = digit
                    continue
                break
        except StopIteration:
            done = True

        # Emit the completed run in the appropriate notation
        if start_digit == stop_digit:
            # Single digit — IOS needs a comma separator between alternatives
            if ios:
                r += ','
            r += start_digit
        else:
            if int(stop_digit) - int(start_digit) == 1:
                # Two consecutive digits: list them individually (e.g. '23')
                if ios:
                    r += ',' + start_digit + ',' + stop_digit
                else:
                    r += start_digit + stop_digit
            else:
                # Three or more consecutive digits: use range notation (e.g. '2-5')
                if ios:
                    r += ',{}-{}'.format(start_digit, stop_digit)
                else:
                    r += '{}-{}'.format(start_digit, stop_digit)
        start_digit = digit

    # Replace the full digit range with the wildcard shorthand
    if r == '0-9':
        r = 'X'

    # Wrap multi-character alternatives in brackets for both UCM and IOS notation
    if ios:
        if len(r) > 2:
            r = '[{}]'.format(r)
        else:
            r = r.replace(',', '')

    if not ios and len(r) > 1:
        r = '[{}]'.format(r)

    # Prepend the five-digit NPA-NXX prefix to form the full six-digit prefix
    r = '{}{}'.format(prefix5d, r)

    # Build the full E.164-anchored pattern, applying optional 7D stripping for HNPA local
    if hnpalocal7d and prefix5d.startswith(home_npa):
        # HNPA local: match on NPA + NXX trailing digit, trailing four digits are wildcarded
        if ios:
            r = '   e164 +1{npa}{trailing}....'.format(npa=home_npa, trailing=r[3:])
        else:
            r = '\\+1{npa}.{trailing}XXXX'.format(npa=home_npa, trailing=r[3:])
    else:
        # FNPA local (or HNPA without 7D stripping): match the full 10D E.164 number
        if ios:
            r = '   e164 +1{trailing}....'.format(trailing=r)
        else:
            r = '\\+1.{trailing}XXXX'.format(trailing=r)

    # IOS bracket expressions must not start with a comma
    if ios:
        r = r.replace('[,', '[')

    return r


def assert_partition(axl, name, read_only=True):
    """Ensure a route partition with the given name exists in UCM, creating it when necessary.

    In read-only mode the partition is looked up but never created; ``None`` is returned if it does
    not exist.

    :param axl: :class:`ucmaxl.AXLHelper` instance used to communicate with UCM
    :param name: name of the route partition to assert
    :param read_only: when ``True``, skip creation and return ``None`` for a missing partition
    :return: UUID string of the existing or newly created partition, or ``None`` in read-only mode
             when the partition does not exist
    """
    p = axl.get_route_partition(name=name)
    if p is not None:
        r = p['uuid']
        print('Partition {} exists.'.format(name))
    else:
        print('Partition {} does not exist.'.format(name))
        if read_only:
            r = None
        else:
            r = axl.add_route_partition(name=name)
            print('Partition {} created.'.format(name))
    return r


def main():
    """Entry point: parse CLI arguments, compute required patterns, and sync them to UCM.

    :return: None
    """
    # --- Argument parsing ---
    args = argparse.ArgumentParser(
        description="""Provision route patterns or called party transformation patterns on UCM
for a given NPA NXX to make sure that NPA/NXXes considered local are treated accordingly.
route patterns or called party transformation patterns matching on local NPA/NXXes are provisioned in a partition
local{npa}{nxx} where {npa} and {nxx} are replaced with the NPA/NXXes of the location. This partition is created if it
doesn't exist already. In this partition patterns other than the ones determined by this script are deleted.

Route patterns and called party transformation patterns are provisioned with a called party transform to transform
HNPA local to 7D and FNPA local 10D.

Route patterns use a route list local{npa}{nxx} where {npa} and {nxx} are replaced with the NPA/NXXes of the location.
This route list has to be created prior to calling the script."""
    )

    args.add_argument('--npa', required=True, help='NPA of the GW location')
    args.add_argument('--nxx', required=True, help='NXX of the GW location')
    args.add_argument('--ucm', required=False, help='IP or FQDN of UCM publisher host')
    args.add_argument('--user', required=False, help='AXL user with write access to UCM')
    args.add_argument('--pwd', required=False, help='Password for AXL user with write access to UCM')
    args.add_argument('--hnpalocal7d', required=False, action='store_true', help='Transform HNPA local destinations to 7D')
    args.add_argument(
        '--routepattern',
        required=False,
        action='store_true',
        help='Provision route patterns. Else called party transforms are provisioned',
    )
    args.add_argument(
        '--readonly', required=False, action='store_true', help="Don't write to UCM. Existing patterns are read if possible."
    )
    args.add_argument(
        '--patternsonly',
        required=False,
        action='store_true',
        help='Only print patterns required. No UCM details nor user credentials are required.',
    )
    args.add_argument(
        '--ios',
        required=False,
        action='store_true',
        help='Creates entries for an IOS e164-pattern-map. Automatically invokes patternsonly parameter as well.',
    )

    parsed_args = args.parse_args()

    # --ios implies --patternsonly; no UCM credentials are needed
    if parsed_args.ios:
        parsed_args.patternsonly = True

    # UCM connection details are required unless we only print patterns
    if not parsed_args.patternsonly and (parsed_args.ucm is None or parsed_args.user is None or parsed_args.pwd is None):
        print('{}: error: the following arguments are required: --ucm --user --pwd'.format(os.path.basename(__file__)))
        exit(2)

    # --- Determine required patterns ---

    # Fetch all NPA-NXX prefixes that are local to the given NPA-NXX
    npanxx = xmllocalprefix(npa=parsed_args.npa, nxx=parsed_args.nxx)
    npanxx.sort()

    # Derive unique five-digit prefixes (NPA + first two NXX digits) to minimise pattern count
    prefixes = list(set((x[:5] for x in npanxx)))
    prefixes.sort()

    # Build one pattern per unique five-digit prefix, grouping the sixth-digit alternatives
    patterns = [
        single_pattern(
            prefix5d,
            ''.join((x[-1] for x in npanxx if x.startswith(prefix5d))),
            parsed_args.npa,
            parsed_args.hnpalocal7d,
            parsed_args.ios,
        )
        for prefix5d in prefixes
    ]
    print('{} patterns are required'.format(len(patterns)))

    # Print-only mode: output the pattern list and exit without touching UCM
    if parsed_args.patternsonly:
        print('\n'.join(patterns))
        return

    # --- Connect to UCM ---
    axl = ucmaxl.AXLHelper(parsed_args.ucm, auth=(parsed_args.user, parsed_args.pwd), version='10.0', verify=False, timeout=60)

    # --- Ensure the target partition exists ---
    npa_nxx_name = 'local{}{}'.format(parsed_args.npa, parsed_args.nxx)
    local_partition = assert_partition(axl, npa_nxx_name, read_only=parsed_args.readonly)

    # --- Select the appropriate UCM object type (route pattern vs. called party transform) ---
    if parsed_args.routepattern:
        # Verify that the required route list exists before provisioning route patterns
        route_list = axl.get_route_list(name=npa_nxx_name)
        if route_list is None and not parsed_args.readonly:
            print('route list {} needs to be created before executing the script'.format(npa_nxx_name))
            exit(2)

        lister = functools.partial(axl.list_route_pattern, routePartitionName=npa_nxx_name)
        adder = functools.partial(
            axl.add_route_pattern,
            routePartitionName=npa_nxx_name,
            digitDiscardInstructionName='PreDot',
            patternUrgency=True,
            blockEnable=False,
            destination={'routeListName': npa_nxx_name},
            networkLocation='OffNet',
            description='local destination in NPA-NXX {}-{}'.format(parsed_args.npa, parsed_args.nxx),
        )
        remover = functools.partial(axl.remove_route_pattern)
    else:
        lister = functools.partial(axl.list_called_party_transformation_pattern, routePartitionName=npa_nxx_name)
        adder = functools.partial(
            axl.add_called_party_transformation_pattern,
            routePartitionName=npa_nxx_name,
            digitDiscardInstructionName='PreDot',
            description='local destination in NPA-NXX {}-{}'.format(parsed_args.npa, parsed_args.nxx),
        )
        remover = functools.partial(axl.remove_called_party_transformation_pattern)

    # --- Read existing UCM objects for the partition ---
    if local_partition is None:
        ucm_objects = []
    else:
        ucm_objects = lister()
    print('{} patterns exist in UCM'.format(len(ucm_objects)))

    # --- Compute the delta between required and existing patterns ---
    ucm_patterns = [p['pattern'] for p in ucm_objects]
    new_patterns = [p for p in patterns if p not in ucm_patterns]
    print('{} new patterns need to be provisioned'.format(len(new_patterns)))

    remove_objects = [o for o in ucm_objects if o['pattern'] not in patterns]
    print('{} patterns need to be removed'.format(len(remove_objects)))

    # --- Apply changes: add new patterns ---
    print('adding patterns...')
    for pattern in new_patterns:
        print('Adding pattern {}'.format(pattern))
        if not parsed_args.readonly:
            adder(pattern=pattern)

    # --- Apply changes: remove obsolete patterns ---
    print('removing patterns...')
    for pattern in remove_objects:
        print('Removing pattern {}'.format(pattern['pattern']))
        if not parsed_args.readonly:
            remover(uuid=pattern['uuid'])


if __name__ == '__main__':
    main()
