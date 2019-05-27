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

import requests
import xmljson
import xml.etree.ElementTree as ET
import ucmaxl
import argparse
import os
import functools


def xmllocalprefix(npa, nxx):
    """
    get list of NPA-NXXes local to given NPA-NXX from localcallingguide.com
    :param npa:
    :param nxx:
    :return:
    """
    url = "https://www.localcallingguide.com/xmllocalprefix.php"
    params = {'npa': npa, 'nxx': nxx}
    response = requests.get(url, params=params)
    data = xmljson.Parker(dict_type=dict).data(ET.fromstring(response.text))
    return ['{npa}{nxx}'.format(npa=prefix['npa'], nxx=prefix['nxx']) for prefix in data['lca-data']['prefix']]


def single_pattern(prefix5d, trailing_digits, home_npa, hnpalocal7d):
    """
    Creates a single pattern
    :param prefix5d: first five digits of npa/nxx
    :param trailing_digits: allowed digits in last digit of npa/nxx
    :param npa: home npa
    :param hnpalocal7d: True, if HNPA local patterns should be stripped to 7D
    :return: single pattern to be provisioned in UCM
    """

    # we want to convert the sequence of allowed trailing digits to (if possible) something like:
    # 1-4
    # X
    r = ''
    i = iter(trailing_digits)
    start_digit = next(i)
    done = False
    while not done:
        # get a sequence
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
        if start_digit == stop_digit:
            # add a single digit
            r += start_digit
        else:
            if int(stop_digit) - int(start_digit) == 1:
                # something like "12"
                r += start_digit
                r += stop_digit
            else:
                # something like "1-3"
                r += '{}-{}'.format(start_digit, stop_digit)
            # if .. else ..
        # if .. else ..
        start_digit = digit
    if r == '0-9':
        r = 'X'
    if len(r) > 1:
        r = '[{}]'.format(r)
    r = '{}{}'.format(prefix5d, r)
    if hnpalocal7d and prefix5d.startswith(home_npa):
        r = '\\+1{npa}.{trailing}XXXX'.format(npa=home_npa, trailing=r[3:])
    else:
        r = '\\+1.{trailing}XXXX'.format(trailing=r)
    return r


def assert_partition(axl, name, read_only=True):
    """
    assert existence of partition w/ given name
    :param axl: AXL helper object
    :param name: partition name
    :param read_only: True=read only access to UCM
    :return: UUID of partition
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
    """
    Main code
    :return: None
    """
    args = argparse.ArgumentParser(description="""Provision route patterns or called party transformation patterns on UCM
for a given NPA NXX to make sure that NPA/NXXes considered local are treated accordingly.
route patterns or called party transformation patterns matching on local NPA/NXXes are provisioned in a partition
local{npa}{nxx} where {npa} and {nxx} are replaced with the NPA/NXXes of the location. This partition is created if it
doesn't exist already. In this partition patterns other than the ones determined by this script are deleted.

Route patterns and called party transformation patterns are provisioned with a called party transform to transform 
HNPA local to 7D and FNPA local 10D.

Route patterns use a route list local{npa}{nxx} where {npa} and {nxx} are replaced with the NPA/NXXes of the location.
This route list has to be created prior to calling the script.""")

    args.add_argument('--npa', required=True, help='NPA of the GW location')
    args.add_argument('--nxx', required=True, help='NXX of the GW location')
    args.add_argument('--ucm', required=False, help='IP or FQDN of UCM publisher host')
    args.add_argument('--user', required=False, help='AXL user with write access to UCM')
    args.add_argument('--pwd', required=False, help='Password for AXL user with write access to UCM')
    args.add_argument('--hnpalocal7d', required=False, action='store_true',
                      help='Transform HNPA local destinations to 7D')
    args.add_argument('--routepattern', required=False, action='store_true',
                      help='Provision route patterns. Else called party transforms are provisioned')
    args.add_argument('--readonly', required=False, action='store_true',
                      help='Don\'t write to UCM. Existing patterns are read if possible.')
    args.add_argument('--patternsonly', required=False, action='store_true',
                      help='Only print patterns required. No UCM details nor user credentials are required.')


    parsed_args = args.parse_args()

    # ucm, user, and pass are required if patternsonly is not set
    if not parsed_args.patternsonly and (parsed_args.ucm is None or parsed_args.user is None or parsed_args.pwd is None):
        # pynanp.py: error: the following arguments are required: --nxx
        print('{}: error: the following arguments are required: --ucm --user --pwd'.format(os.path.basename(__file__)))
        exit(2)

    # get list of local NPA-NXXes
    npanxx = xmllocalprefix(npa=parsed_args.npa, nxx=parsed_args.nxx)
    npanxx.sort()

    # list of 5D prefixes
    prefixes = list(set((x[:5] for x in npanxx)))
    prefixes.sort()

    # required patterns
    patterns = [single_pattern(prefix5d,
                               ''.join((x[-1] for x in npanxx if x.startswith(prefix5d))),
                               parsed_args.npa,
                               parsed_args.hnpalocal7d) for prefix5d in prefixes]
    print('{} patterns are required'.format(len(patterns)))

    # if only a list of patterns is required then print the list of patterns and return
    if parsed_args.patternsonly:
        print('\n'.join(patterns))
        return

    # AXL helper object
    axl = ucmaxl.AXLHelper(parsed_args.ucm, auth=(parsed_args.user, parsed_args.pwd), version='10.0', verify=False,
                           timeout=60)

    # assert existence of partition local{npa}{nxx}
    npa_nxx_name = 'local{}{}'.format(parsed_args.npa, parsed_args.nxx)
    local_partition = assert_partition(axl, npa_nxx_name, read_only=parsed_args.readonly)

    if parsed_args.routepattern:
        # assert existence of route list
        route_list = axl.get_route_list(name=npa_nxx_name)
        if route_list is None and not parsed_args.readonly:
            print('route list {} needs to be created before executing the script'.format(npa_nxx_name))
            exit(2)
        # set methods for route patterns
        lister = functools.partial(axl.list_route_pattern,
                                   routePartitionName=npa_nxx_name)

        adder = functools.partial(axl.add_route_pattern,
                                  routePartitionName=npa_nxx_name,
                                  digitDiscardInstructionName='PreDot',
                                  patternUrgency=True,
                                  blockEnable=False,
                                  destination={'routeListName':npa_nxx_name},
                                  networkLocation='OffNet',
                                  description='local destination in NPA-NXX {}-{}'.format(parsed_args.npa,
                                                                                          parsed_args.nxx))
        remover = functools.partial(axl.remove_route_pattern)
    else:
        # methods for called party transformation patterns
        lister = functools.partial(axl.list_called_party_transformation_pattern,
                                   routePartitionName=npa_nxx_name)
        adder = functools.partial(axl.add_called_party_transformation_pattern,
                                  routePartitionName=npa_nxx_name,
                                  digitDiscardInstructionName='PreDot',
                                  description='local destination in NPA-NXX {}-{}'.format(parsed_args.npa, parsed_args.nxx))
        remover = functools.partial(axl.remove_called_party_transformation_pattern)

    # get all called patterns in that partition
    if local_partition is None:
        ucm_objects = []
    else:
        ucm_objects = lister()
    print('{} patterns exist in UCM'.format(len(ucm_objects)))

    # determine patterns to be added/removed
    ucm_patterns = [p['pattern'] for p in ucm_objects]
    new_patterns = [p for p in patterns if p not in ucm_patterns]
    print('{} new patterns need to be provisioned'.format(len(new_patterns)))

    remove_objects = [o for o in ucm_objects if o['pattern'] not in patterns]
    print('{} patterns need to be removed'.format(len(remove_objects)))

    # add new patterns
    print('adding patterns...')
    for pattern in new_patterns:
        print('Adding pattern {}'.format(pattern))
        if not parsed_args.readonly:
            adder(pattern=pattern)

    # remove patterns not needed any more
    print('removing patterns...')
    for pattern in remove_objects:
        print('Removing pattern {}'.format(pattern['pattern']))
        if not parsed_args.readonly:
            remover(uuid=pattern['uuid'])

if __name__ == '__main__':
    main()
