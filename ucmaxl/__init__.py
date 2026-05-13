"""
AXL helper to wrap SOAP methods created based on the UCM AXL WSDL.

All public methods follow one of two naming conventions:

* ``get_*/list_*`` — read-only lookups; return ``None`` or ``[]`` when nothing is found.
* ``add_update_*`` — idempotent upserts; create the object if it does not exist, update it otherwise.
"""

import os
import tempfile
from collections import OrderedDict

import requests
import zeep
import zeep.cache
import zeep.exceptions
import zeep.helpers


class AXLHelper:
    def __init__(self, ucm_host, auth, version=None, verify=None, timeout=60):
        """Initialise a zeep-based AXL SOAP client for the given UCM host.

        :param ucm_host: IP address or FQDN of the UCM publisher; a port may be appended
                         (e.g. ``'cucm.example.com:8443'``); defaults to port 8443 when omitted
        :param auth: authentication object passed to the underlying ``requests.Session``; for basic
                     authentication pass a ``(username, password)`` tuple
        :param version: WSDL schema version string matching an ``AXLAPI.wsdl`` under the ``WSDL/``
                        directory (e.g. ``'12.0'``); required — raises ``Exception`` when ``None``
        :param verify: SSL certificate verification flag forwarded to ``requests.Session.verify``;
                       pass ``False`` to disable certificate validation for self-signed certificates
        :param timeout: socket timeout in seconds applied to both connection and operation calls
        """
        self.ucm_host = ucm_host
        if ':' not in ucm_host:
            ucm_host += ':8443'
        self.axl_url = 'https://{ucm_host}/axl/'.format(ucm_host=ucm_host)

        # Build a requests session with credentials and optional SSL override
        self.session = requests.Session()
        self.session.auth = auth
        if verify is not None:
            self.session.verify = verify

        if version is None:
            raise Exception('Not implemented')

        # Locate the WSDL file bundled alongside this package
        wsdl_version = version
        self.wsdl = os.path.join(os.path.dirname(__file__), 'WSDL', wsdl_version, 'AXLAPI.wsdl')

        # Use a file-based SQLite cache so parsed WSDL/XSD data survives restarts
        self.cache = zeep.cache.SqliteCache(
            path=os.path.join(tempfile.gettempdir(), 'sqlite_{}.db'.format(self.ucm_host)), timeout=60
        )

        # Instantiate the zeep client and bind it to the AXL service endpoint
        self.client = zeep.Client(
            wsdl=self.wsdl,
            transport=zeep.Transport(timeout=timeout, operation_timeout=timeout, cache=self.cache, session=self.session),
        )
        self.service = self.client.create_service('{http://www.cisco.com/AXLAPIService/}AXLAPIBinding', self.axl_url)
        return

    def __getattr__(self, item):
        """Delegate unknown attribute lookups to the underlying zeep service proxy.

        This allows callers to invoke any AXL operation directly on the helper instance as if it
        were a method (e.g. ``axl.getPhone(...)``).

        :param item: name of the AXL operation to look up on the zeep service
        :return: the corresponding zeep service operation callable
        """
        return self.service[item]

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def filter_search_criteria(search_criteria, supported_search_criteria, default_search_criteria=None):
        """Strip unsupported keys from a search-criteria dict and apply a default when it is empty.

        :param search_criteria: dict of candidate key/value pairs supplied by the caller
        :param supported_search_criteria: iterable of key names accepted by the AXL operation
        :param default_search_criteria: key to populate with ``'%'`` (match-all wildcard) when
                                        *search_criteria* is empty after filtering; when ``None``
                                        and the filtered dict is empty, ``None`` is returned
        :return: filtered dict ready to pass as ``searchCriteria``, or ``None`` when filtering
                 yields an empty dict and no default is specified
        """
        search_criteria = {k: v for k, v in search_criteria.items() if k in supported_search_criteria}
        if not search_criteria:
            if default_search_criteria is None:
                return None
            search_criteria[default_search_criteria] = '%'
        return search_criteria

    @staticmethod
    def handle_list_response(r):
        """Extract and deserialise the item list from a zeep ``list*`` response envelope.

        :param r: raw zeep response dict whose ``'return'`` key holds the list payload
        :return: list of plain dicts (zeep objects serialised via ``serialize_object``), or ``[]``
                 when the response contains no items
        """
        if r['return'] is None:
            return []
        # The first key inside 'return' is the item-type name (e.g. 'routePattern')
        r = r['return'][next((r for r in r['return']))]
        return [zeep.helpers.serialize_object(s) for s in r]

    # -------------------------------------------------------------------------
    # SQL passthrough
    # -------------------------------------------------------------------------

    def sql_query(self, query):
        """Execute a raw Informix SQL query against the UCM database.

        :param query: SQL SELECT statement to execute
        :return: list of :class:`~collections.OrderedDict` instances, one per result row,
                 with column names as keys; returns ``[]`` when the query yields no rows
        """
        r = self.service.executeSQLQuery(sql=query)

        if r['return'] is None:
            return []

        return [OrderedDict(((t.tag, t.text) for t in row)) for row in r['return']['row']]

    def sql_update(self, sql):
        """Execute a raw Informix SQL DML statement against the UCM database.

        :param sql: SQL UPDATE, INSERT, or DELETE statement to execute
        :return: number of rows affected by the statement
        """
        r = self.service.executeSQLUpdate(sql=sql)
        return r['return']['rowsUpdated']

    # -------------------------------------------------------------------------
    # Device reset
    # -------------------------------------------------------------------------

    def do_device_reset(self, name, is_mgcp=False, is_hard_reset=False, reset_type='Reset'):
        """Send a device reset/restart/apply-config command to a UCM device.

        :param name: device name as configured in UCM
        :param is_mgcp: ``True`` if the device is an MGCP gateway
        :param is_hard_reset: ``True`` to perform a hard reset rather than a graceful one
        :param reset_type: reset action to perform; one of ``'Reset'``, ``'Restart'``, or
                           ``'Apply Configuration'``
        :return: raw zeep response from the ``doDeviceReset`` AXL operation
        """
        return self.service.doDeviceReset(isMGCP=is_mgcp, deviceName=name, isHardReset=is_hard_reset, deviceResetType=reset_type)

    # -------------------------------------------------------------------------
    # Service parameters
    # -------------------------------------------------------------------------

    def get_service_parameter(self, process_node_name, name, service):
        """Retrieve a single UCM service parameter.

        :param process_node_name: UCM node name (e.g. ``'EnterpriseWideData'`` for enterprise-wide
                                  parameters, or the FQDN of a specific node)
        :param name: parameter name
        :param service: service name the parameter belongs to (e.g. ``'Enterprise Wide'``)
        :return: dict representation of the ``serviceParameter`` AXL object
        """
        tags = ['name', 'service', 'value', 'valueType', 'processNodeName']
        r = self.service.getServiceParameter(
            processNodeName=process_node_name, name=name, service=service, returnedTags={t: '' for t in tags}
        )
        return r['return']['serviceParameter']

    def get_enterprise_parameter(self, name):
        """Retrieve a single enterprise-wide UCM service parameter.

        Convenience wrapper around :meth:`get_service_parameter` that fixes the node and service
        to the enterprise-wide defaults.

        :param name: parameter name
        :return: dict representation of the ``serviceParameter`` AXL object
        """
        return self.get_service_parameter(process_node_name='EnterpriseWideData', service='Enterprise Wide', name=name)

    def update_service_parameter(self, process_node_name, name, service, value):
        """Update a single UCM service parameter value.

        :param process_node_name: UCM node name (e.g. ``'EnterpriseWideData'``)
        :param name: parameter name
        :param service: service name the parameter belongs to
        :param value: new value to set
        :return: UUID of the updated parameter record
        """
        r = self.service.updateServiceParameter(processNodeName=process_node_name, name=name, service=service, value=value)
        return r['return']

    def update_enterprise_parameter(self, name, value):
        """Update a single enterprise-wide UCM service parameter value.

        Convenience wrapper around :meth:`update_service_parameter` that fixes the node and service
        to the enterprise-wide defaults.

        :param name: parameter name
        :param value: new value to set
        :return: UUID of the updated parameter record
        """
        return self.update_service_parameter(
            process_node_name='EnterpriseWideData', service='Enterprise Wide', name=name, value=value
        )

    def list_service_parameter(self, **search_criteria):
        """List UCM service parameters matching the given search criteria.

        :param search_criteria: supported keys are ``processNodeName`` and ``service``; when no
                                criteria are supplied, all parameters for all nodes are returned
        :return: list of service parameter dicts
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['processNodeName', 'service'], 'processNodeName')

        tags = ['processNodeName', 'name', 'service', 'value', 'valueType']
        r = self.service.listServiceParameter(searchCriteria=search_criteria, returnedTags={t: '' for t in tags})
        return self.handle_list_response(r)

    # -------------------------------------------------------------------------
    # Calling search spaces
    # -------------------------------------------------------------------------

    def list_css(self, **search_criteria):
        """List calling search spaces (CSS) matching the given search criteria.

        :param search_criteria: supported keys are ``description``, ``partitionUsage``, and ``name``
        :return: list of CSS dicts containing description, clause, dialPlanWizardGenId,
                 partitionUsage, and name
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['description', 'partitionUsage', 'name'], 'name')
        tags = ['description', 'clause', 'dialPlanWizardGenId', 'partitionUsage', 'name']
        r = self.service.listCss(searchCriteria=search_criteria, returnedTags={t: '' for t in tags})
        return self.handle_list_response(r)

    def add_update_css(self, name, description, clause):
        """Create or update a calling search space (CSS).

        The *clause* is a colon-separated ordered list of partition names, e.g.
        ``'Internal:Local:National'``.  Members are rebuilt from scratch on every update.

        :param name: CSS name
        :param description: human-readable description
        :param clause: colon-separated partition names defining the search order
        :return: UUID of the created or updated CSS
        """
        # Build the ordered member list from the colon-delimited clause string
        member_list = [{'routePartitionName': p, 'index': i} for i, p in enumerate(clause.split(':'), start=1)]
        css = {'name': name, 'description': description, 'members': {'member': member_list}}
        try:
            p = self.service.getCss(name=name, returnedTags={'description': '', 'clause': ''})
        except zeep.exceptions.Fault:
            p = self.service.addCss(css=css)
        else:
            p = self.service.updateCss(name=name, description=description, members={'member': member_list})

        return p['return']

    # -------------------------------------------------------------------------
    # Process nodes
    # -------------------------------------------------------------------------

    def list_process_node(self, **search_criteria):
        """List UCM cluster nodes matching the given search criteria.

        :param search_criteria: supported keys are ``name``, ``description``, and
                                ``processNodeRole``; defaults to listing all nodes when empty
        :return: list of process node dicts (note: ``processNodeRole`` is intentionally excluded
                 from returned tags due to a zeep parsing bug with that field)
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['name', 'description', 'processNodeRole'], 'name')
        # processNodeRole is omitted from returned tags — zeep fails to parse
        # <processNodeRole>CUCM Voice/Video</processNodeRole> due to the slash
        tags = ['name', 'description', 'mac', 'ipv6Name', 'nodeUsage', 'lbmHubGroup']
        r = self.service.listProcessNode(search_criteria, returnedTags={t: '' for t in tags})
        return self.handle_list_response(r)

    def update_process_node(self, name=None, uuid=None, new_name=None):
        """Rename a UCM cluster node via a direct SQL update.

        Either *name* or *uuid* must be supplied, but not both.  The rename is performed through
        ``executeSQLUpdate`` because the thick AXL ``updateProcessNode`` method is unreliable for
        this operation.

        :param name: current node name (mutually exclusive with *uuid*)
        :param uuid: UUID of the node in curly-bracket uppercase form as returned by thick AXL
                     (mutually exclusive with *name*)
        :param new_name: new node name to set; required
        :return: number of database rows updated
        """
        assert new_name is not None
        if name is not None:
            assert uuid is None
            key, value = 'name', name
        else:
            assert uuid is not None
            # UUIDs from thick AXL are uppercase and wrapped in curly brackets; normalise to lowercase
            key, value = 'pkid', uuid[1:-1].lower()

        sql = "update processnode set name='{new_name}' where {key}='{value}'".format(new_name=new_name, key=key, value=value)
        r = self.sql_update(sql=sql)
        return r

    # -------------------------------------------------------------------------
    # Users
    # -------------------------------------------------------------------------

    def list_user(self, **search_criteria):
        """List end users matching the given search criteria.

        :param search_criteria: supported filter keys are ``firstName``, ``lastName``, ``userid``,
                                and ``department``; pass ``returnedTags`` as a dict to override the
                                default set of returned fields
        :return: list of user dicts; default fields are uuid, userid, firstName, lastName
        """
        returned_tags = search_criteria.pop('returnedTags', None)
        search_criteria = self.filter_search_criteria(
            search_criteria, ['firstName', 'lastName', 'userid', 'department'], 'userid'
        )
        if returned_tags is None:
            returned_tags = {'uuid': '', 'userid': '', 'firstName': '', 'lastName': ''}
        r = self.service.listUser(searchCriteria=search_criteria, returnedTags=returned_tags)
        return self.handle_list_response(r)

    # -------------------------------------------------------------------------
    # Route partitions
    # -------------------------------------------------------------------------

    def list_route_partition(self, **search_criteria):
        """List route partitions matching the given search criteria.

        :param search_criteria: supported keys are ``name`` and ``description``
        :return: list of route partition dicts
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['name', 'description'], 'name')
        tags = [
            'name',
            'description',
            'dialPlanWizardGenId',
            'timeScheduleIdName',
            'useOriginatingDeviceTimeZone',
            'timeZone',
            'partitionUsage',
        ]
        r = self.service.listRoutePartition(searchCriteria=search_criteria, returnedTags={t: '' for t in tags})
        return self.handle_list_response(r)

    def get_route_partition(self, **search_criteria):
        """Retrieve a single route partition by name or UUID.

        :param search_criteria: exactly one of ``name`` or ``uuid``
        :return: route partition dict, or ``None`` when the partition does not exist
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['name', 'uuid'], 'name')
        assert search_criteria is not None, 'Search criteria mantatory'
        assert len(search_criteria) == 1, 'Only name or uuid can be used'

        tags = [
            'name',
            'description',
            'dialPlanWizardGenId',
            'timeScheduleIdName',
            'useOriginatingDeviceTimeZone',
            'timeZone',
            'partitionUsage',
        ]
        try:
            r = self.service.getRoutePartition(returnedTags={t: '' for t in tags}, **search_criteria)
        except zeep.exceptions.Fault as e:
            if e.message.startswith('Item not valid'):
                return None
            raise
        r = zeep.helpers.serialize_object(r['return']['routePartition'])
        return r

    def add_route_partition(self, **values):
        """Create a new route partition.

        :param values: AXL ``routePartition`` field values (e.g. ``name``, ``description``)
        :return: UUID of the newly created route partition
        """
        r = self.service.addRoutePartition(routePartition=values)
        return r['return']

    def update_route_partition(self, **values):
        """Update an existing route partition.

        :param values: AXL ``updateRoutePartition`` parameters; must include ``name`` or ``uuid``
        :return: UUID of the updated route partition
        """
        r = self.service.updateRoutePartition(**values)
        return r['return']

    def add_update_route_partition(self, name, description):
        """Create or update a route partition with the given name and description.

        :param name: partition name
        :param description: human-readable description
        :return: UUID of the created or updated route partition
        """
        p = self.get_route_partition(name=name)
        if p is None:
            p = self.add_route_partition(name=name, description=description)
        else:
            p = self.update_route_partition(name=name, description=description)
        return p

    # -------------------------------------------------------------------------
    # Route lists
    # -------------------------------------------------------------------------

    def get_route_list(self, **search_criteria):
        """Retrieve a single route list by name or UUID.

        :param search_criteria: exactly one of ``name`` or ``uuid``
        :return: route list dict, or ``None`` when the route list does not exist
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['name', 'uuid'], 'name')
        assert search_criteria is not None, 'Search criteria mantatory'
        assert len(search_criteria) == 1, 'Only name or uuid can be used'

        tags = ['name', 'description', 'callManagerGroupName', 'routeListEnabled']
        try:
            r = self.service.getRouteList(returnedTags={t: '' for t in tags}, **search_criteria)
        except zeep.exceptions.Fault as e:
            if e.message.startswith('Item not valid'):
                return None
            raise
        r = zeep.helpers.serialize_object(r['return']['routeList'])
        return r

    # -------------------------------------------------------------------------
    # Route patterns
    # -------------------------------------------------------------------------

    ROUTE_PATTERN_TAGS = [
        'pattern',
        'description',
        'usage',
        'routePartitionName',
        'blockEnable',
        'calledPartyTransformationMask',
        'callingPartyTransformationMask',
        'useCallingPartyPhoneMask',
        'callingPartyPrefixDigits',
        'dialPlanName',
        'dialPlanWizardGenId',
        'digitDiscardInstructionName',
        'networkLocation',
        'patternUrgency',
        'prefixDigitsOut',
        'routeFilterName',
        'callingLinePresentationBit',
        'callingNamePresentationBit',
        'connectedLinePresentationBit',
        'connectedNamePresentationBit',
        'supportOverlapSending',
        'patternPrecedence',
        'releaseClause',
        'allowDeviceOverride',
        'provideOutsideDialtone',
        'callingPartyNumberingPlan',
        'callingPartyNumberType',
        'calledPartyNumberingPlan',
        'calledPartyNumberType',
        'authorizationCodeRequired',
        'authorizationLevelRequired',
        'clientCodeRequired',
        'withTag',
        'withValueClause',
        'resourcePriorityNamespaceName',
        'routeClass',
        'externalCallControl',
    ]

    def list_route_pattern(self, **search_criteria):
        """List route patterns matching the given search criteria.

        :param search_criteria: supported keys are ``pattern``, ``description``, and
                                ``routePartitionName``
        :return: list of route pattern dicts
        """
        search_criteria = self.filter_search_criteria(
            search_criteria, ['pattern', 'description', 'routePartitionName'], 'pattern'
        )
        r = self.service.listRoutePattern(searchCriteria=search_criteria, returnedTags={t: '' for t in self.ROUTE_PATTERN_TAGS})
        return self.handle_list_response(r)

    def get_route_pattern(self, **search_criteria):
        """Retrieve a single route pattern by UUID, pattern string, or partition.

        :param search_criteria: any combination of ``uuid``, ``pattern``, and ``routePartitionName``
        :return: route pattern dict, or ``None`` when the pattern does not exist
        """
        search_criteria = self.filter_search_criteria(search_criteria, ['uuid', 'pattern', 'routePartitionName'])
        assert search_criteria is not None, 'Search criteria mantatory'

        try:
            r = self.service.getRoutePattern(returnedTags={t: '' for t in self.ROUTE_PATTERN_TAGS}, **search_criteria)
        except zeep.exceptions.Fault as e:
            if e.message.startswith('Item not valid'):
                return None
            raise
        r = zeep.helpers.serialize_object(r['return']['routePattern'])
        return r

    def add_route_pattern(self, **values):
        """Create a new route pattern.

        :param values: AXL ``routePattern`` field values
        :return: UUID of the newly created route pattern
        """
        r = self.service.addRoutePattern(routePattern=values)
        return r['return']

    def update_route_pattern(self, **values):
        """Update an existing route pattern.

        :param values: AXL ``updateRoutePattern`` parameters; must include ``pattern`` or ``uuid``
        :return: UUID of the updated route pattern
        """
        r = self.service.updateRoutePattern(**values)
        return r['return']

    def add_update_route_pattern(self, pattern, partition, description, route_list_name):
        """Create or update a route pattern pointing to the given route list.

        :param pattern: dial-plan pattern string (e.g. ``'\\+1.XXXXXXXXXX'``)
        :param partition: name of the route partition the pattern belongs to
        :param description: human-readable description
        :param route_list_name: name of the route list to use as the destination
        :return: UUID of the created or updated route pattern
        """
        route_pattern = {
            'pattern': pattern,
            'routePartitionName': partition,
            'description': description,
            'blockEnable': 'false',
            'calledPartyTransformationMask': '',
            'callingPartyTransformationMask': '',
            'useCallingPartyPhoneMask': 'Off',
            'callingPartyPrefixDigits': '',
            'digitDiscardInstructionName': '',
            'patternUrgency': 'false',
            'prefixDigitsOut': '',
            'routeFilterName': '',
            'supportOverlapSending': 'false',
            'patternPrecedence': 'Default',
            'provideOutsideDialtone': 'true',
            'authorizationCodeRequired': 'false',
            'authorizationLevelRequired': '0',
            'externalCallControl': '',
            'destination': {'routeListName': route_list_name},
        }
        p = self.get_route_pattern(pattern=pattern, routePartitionName=partition)
        if p is None:
            p = self.add_route_pattern(**route_pattern)
        else:
            p = self.update_route_pattern(**route_pattern)
        return p

    def remove_route_pattern(self, uuid):
        """Delete a route pattern by UUID.

        :param uuid: UUID of the route pattern to remove
        :return: raw zeep response from the ``removeRoutePattern`` AXL operation
        """
        r = self.service.removeRoutePattern(uuid=uuid)
        return r

    # -------------------------------------------------------------------------
    # Called party transformation patterns
    # -------------------------------------------------------------------------

    CDPTX_TAGS = [
        'pattern',
        'description',
        'usage',
        'routePartitionName',
        'calledPartyTransformationMask',
        'dialPlanName',
        'digitDiscardInstructionName',
        'patternUrgency',
        'routeFilterName',
        'calledPartyPrefixDigits',
        'calledPartyNumberingPlan',
        'calledPartyNumberType',
        'mlppPreemptionDisabled',
    ]

    def list_called_party_transformation_pattern(self, **search_criteria):
        """List called party transformation patterns matching the given search criteria.

        :param search_criteria: supported keys are ``pattern``, ``description``,
                                ``routePartitionName``, ``dialPlanName``, and ``routeFilterName``
        :return: list of called party transformation pattern dicts
        """
        search_criteria = self.filter_search_criteria(
            search_criteria, ['pattern', 'description', 'routePartitionName', 'dialPlanName', 'routeFilterName'], 'pattern'
        )
        r = self.service.listCalledPartyTransformationPattern(
            searchCriteria=search_criteria, returnedTags={t: '' for t in self.CDPTX_TAGS}
        )
        return self.handle_list_response(r)

    def add_called_party_transformation_pattern(self, **values):
        """Create a new called party transformation pattern.

        :param values: AXL ``calledPartyTransformationPattern`` field values
        :return: UUID of the newly created pattern
        """
        r = self.service.addCalledPartyTransformationPattern(calledPartyTransformationPattern=values)
        return r['return']

    def remove_called_party_transformation_pattern(self, uuid):
        """Delete a called party transformation pattern by UUID.

        :param uuid: UUID of the pattern to remove
        :return: raw zeep response from the ``removeCalledPartyTransformationPattern`` AXL operation
        """
        r = self.service.removeCalledPartyTransformationPattern(uuid=uuid)
        return r

    # -------------------------------------------------------------------------
    # SIP profiles
    # -------------------------------------------------------------------------

    SIP_PROFILE_TAGS = ['name', 'description']

    def get_sip_profile(self, name):
        """Retrieve a SIP profile by name.

        :param name: SIP profile name
        :return: SIP profile dict, or ``None`` when the profile does not exist
        """
        try:
            r = self.service.getSipProfile(returnedTags={t: '' for t in self.SIP_PROFILE_TAGS}, name=name)
        except zeep.exceptions.Fault as e:
            if e.message.startswith('Item not valid'):
                return None
            raise
        r = zeep.helpers.serialize_object(r['return']['sipProfile'])
        return r

    def add_sip_profile(self, sip_profile):
        """Create a new SIP profile.

        :param sip_profile: dict of AXL ``sipProfile`` field values
        :return: UUID of the newly created SIP profile
        """
        r = self.service.addSipProfile(sipProfile=sip_profile)
        return r['return']

    def update_sip_profile(self, **values):
        """Update an existing SIP profile.

        :param values: AXL ``updateSipProfile`` parameters; must include ``name`` or ``uuid``
        :return: UUID of the updated SIP profile
        """
        r = self.service.updateSipProfile(**values)
        return r['return']

    def add_update_sip_profile(self, sip_profile):
        """Create or update a SIP profile, merging caller-supplied values over sensible defaults.

        The *sip_profile* dict must contain at least a ``name`` key.  All other fields fall back to
        the standard UCM SIP profile defaults defined inside this method.

        :param sip_profile: dict of SIP profile fields to set; overrides the built-in defaults
        :return: UUID of the created or updated SIP profile
        """
        standard_sip_profile = {
            'defaultTelephonyEventPayloadType': '101',
            'redirectByApplication': 'false',
            'ringing180': 'false',
            'timerInvite': '180',
            'timerRegisterDelta': '5',
            'timerRegister': '3600',
            'timerT1': '500',
            'timerT2': '4000',
            'retryInvite': '6',
            'retryNotInvite': '10',
            'startMediaPort': '16384',
            'stopMediaPort': '32766',
            'startVideoPort': '0',
            'stopVideoPort': '0',
            'dscpForAudioCalls': '',
            'dscpForVideoCalls': '',
            'dscpForAudioPortionOfVideoCalls': '',
            'dscpForTelePresenceCalls': '',
            'dscpForAudioPortionOfTelePresenceCalls': '',
            'callpickupListUri': 'x-cisco-serviceuri-opickup',
            'callpickupGroupUri': 'x-cisco-serviceuri-gpickup',
            'meetmeServiceUrl': 'x-cisco-serviceuri-meetme',
            'userInfo': 'None',
            'dtmfDbLevel': 'Nominal',
            'callHoldRingback': 'Off',
            'anonymousCallBlock': 'Off',
            'callerIdBlock': 'Off',
            'dndControl': 'User',
            'telnetLevel': 'Disabled',
            'timerKeepAlive': '120',
            'timerSubscribe': '120',
            'timerSubscribeDelta': '5',
            'maxRedirects': '70',
            'timerOffHookToFirstDigit': '15000',
            'callForwardUri': 'x-cisco-serviceuri-cfwdall',
            'abbreviatedDialUri': 'x-cisco-serviceuri-abbrdial',
            'confJointEnable': 'true',
            'rfc2543Hold': 'false',
            'semiAttendedTransfer': 'true',
            'enableVad': 'false',
            'stutterMsgWaiting': 'false',
            'callStats': 'false',
            't38Invite': 'false',
            'faxInvite': 'false',
            'rerouteIncomingRequest': 'Never',
            'resourcePriorityNamespaceListName': '',
            'enableAnatForEarlyOfferCalls': 'false',
            'rsvpOverSip': 'Local RSVP',
            'fallbackToLocalRsvp': 'true',
            'sipRe11XxEnabled': 'Disabled',
            'gClear': 'Disabled',
            'sendRecvSDPInMidCallInvite': 'false',
            'enableOutboundOptionsPing': 'false',
            'optionsPingIntervalWhenStatusOK': '60',
            'optionsPingIntervalWhenStatusNotOK': '120',
            'deliverConferenceBridgeIdentifier': 'false',
            'sipOptionsRetryCount': '6',
            'sipOptionsRetryTimer': '500',
            'sipBandwidthModifier': 'TIAS and AS',
            'enableUriOutdialSupport': 'f',
            'userAgentServerHeaderInfo': 'Send Unified CM Version Information as User-Agent Header',
            'allowPresentationSharingUsingBfcp': 'false',
            'scriptParameters': '',
            'isScriptTraceEnabled': 'false',
            'sipNormalizationScript': '',
            'allowiXApplicationMedia': 'false',
            'dialStringInterpretation': 'Phone number consists of characters 0-9, *, #, and + (others treated as URI addresses)',
            'acceptAudioCodecPreferences': 'Default',
            'mlppUserAuthorization': 'false',
            'isAssuredSipServiceEnabled': 'false',
            'enableExternalQoS': 'false',
            'resourcePriorityNamespace': '',
            'useCallerIdCallerNameinUriOutgoingRequest': 'false',
            'callerIdDn': '',
            'callerName': '',
            'callingLineIdentification': 'Default',
            'rejectAnonymousIncomingCall': 'false',
            'callpickupUri': 'x-cisco-serviceuri-pickup',
            'rejectAnonymousOutgoingCall': 'false',
            'videoCallTrafficClass': 'Mixed',
            'sdpTransparency': '',
            'allowMultipleCodecs': 'false',
            'sipSessionRefreshMethod': 'Invite',
            'earlyOfferSuppVoiceCall': 'Disabled (Default value)',
            'cucmVersionInSipHeader': 'Major And Minor',
            'confidentialAccessLevelHeaders': 'Disabled',
            'destRouteString': 'false',
            'inactiveSDPRequired': 'false',
            'allowRRAndRSBandwidthModifier': 'false',
            'connectCallBeforePlayingAnnouncement': 'false',
        }
        # Merge caller-supplied values over the defaults
        profile = dict(standard_sip_profile)
        profile.update(**sip_profile)
        sip_profile = profile
        p = self.get_sip_profile(name=sip_profile['name'])
        if p is None:
            p = self.add_sip_profile(sip_profile)
        else:
            p = self.update_sip_profile(**sip_profile)
        return p

    # -------------------------------------------------------------------------
    # Translation patterns
    # -------------------------------------------------------------------------

    TRANS_PATTERN_TAGS = ['pattern', 'description', 'routePartitionName']

    def list_translation(self, **search_criteria):
        """List translation patterns matching the given search criteria.

        :param search_criteria: supported keys are ``pattern``, ``description``, and
                                ``routePartitionName``
        :return: list of translation pattern dicts
        """
        search_criteria = self.filter_search_criteria(
            search_criteria, ['pattern', 'description', 'routePartitionName'], 'pattern'
        )
        r = self.service.listTransPattern(searchCriteria=search_criteria, returnedTags={t: '' for t in self.TRANS_PATTERN_TAGS})
        return self.handle_list_response(r)

    def add_translation(
        self,
        pattern,
        partition,
        description,
        digit_discard='',
        prefix_digits='',
        called_party_transformation_mask='',
        block_enable=False,
        urgency=True,
        outside_dial_tone=False,
        css_inheritance=True,
        dont_wait_for_idt=True,
    ):
        """Create a new translation pattern.

        :param pattern: dial-plan pattern string
        :param partition: name of the route partition the pattern belongs to
        :param description: human-readable description
        :param digit_discard: digit discard instruction name (e.g. ``'PreDot'``)
        :param prefix_digits: digits to prepend to the transformed number
        :param called_party_transformation_mask: called party number mask to apply
        :param block_enable: when ``True``, the pattern blocks the call
        :param urgency: when ``True``, UCM treats this pattern with urgency (overlap sending)
        :param outside_dial_tone: when ``True``, UCM provides an outside dial tone
        :param css_inheritance: when ``True``, the originator CSS is inherited for subsequent hops
        :param dont_wait_for_idt: when ``True``, UCM does not wait for an inter-digit timeout
        :return: raw zeep response from the ``addTransPattern`` AXL operation
        """
        translation = {
            'pattern': pattern,
            'routePartitionName': partition,
            'description': description,
            'usage': 'Translation',
            'blockEnable': block_enable,
            'patternUrgency': urgency,
            'provideOutsideDialtone': outside_dial_tone,
            'digitDiscardInstructionName': digit_discard,
            'prefixDigitsOut': prefix_digits,
            'useOriginatorCss': css_inheritance,
            'dontWaitForIDTOnSubsequentHops': dont_wait_for_idt,
            'calledPartyTransformationMask': called_party_transformation_mask,
        }
        r = self.service.addTransPattern(transPattern=translation)
        return r

    def add_update_translation(
        self,
        pattern,
        partition,
        description,
        digit_discard='',
        prefix_digits='',
        called_party_transformation_mask='',
        block_enable=False,
        urgency=True,
        outside_dial_tone=False,
        css_inheritance=True,
        dont_wait_for_idt=True,
    ):
        """Create or update a translation pattern.

        Parameters are identical to :meth:`add_translation`.

        :param pattern: dial-plan pattern string
        :param partition: name of the route partition the pattern belongs to
        :param description: human-readable description
        :param digit_discard: digit discard instruction name
        :param prefix_digits: digits to prepend to the transformed number
        :param called_party_transformation_mask: called party number mask to apply
        :param block_enable: when ``True``, the pattern blocks the call
        :param urgency: when ``True``, UCM treats this pattern with urgency
        :param outside_dial_tone: when ``True``, UCM provides an outside dial tone
        :param css_inheritance: when ``True``, the originator CSS is inherited for subsequent hops
        :param dont_wait_for_idt: when ``True``, UCM does not wait for an inter-digit timeout
        :return: UUID of the created or updated translation pattern
        """
        translation = {
            'pattern': pattern,
            'routePartitionName': partition,
            'description': description,
            'usage': 'Translation',
            'blockEnable': block_enable,
            'patternUrgency': urgency,
            'provideOutsideDialtone': outside_dial_tone,
            'digitDiscardInstructionName': digit_discard,
            'prefixDigitsOut': prefix_digits,
            'useOriginatorCss': css_inheritance,
            'dontWaitForIDTOnSubsequentHops': dont_wait_for_idt,
            'calledPartyTransformationMask': called_party_transformation_mask,
        }
        try:
            p = self.service.getTransPattern(
                pattern=pattern, routePartitionName=partition, returnedTags={'pattern': '', 'routePartitionName': ''}
            )
        except zeep.exceptions.Fault:
            p = self.service.addTransPattern(transPattern=translation)
        else:
            # 'usage' is not accepted by updateTransPattern
            translation.pop('usage', None)
            p = self.service.updateTransPattern(**translation)
        return p['return']

    def remove_translation(self, uuid):
        """Delete a translation pattern by UUID.

        :param uuid: UUID of the translation pattern to remove
        :return: raw zeep response from the ``removeTransPattern`` AXL operation
        """
        r = self.service.removeTransPattern(uuid=uuid)
        return r

    # -------------------------------------------------------------------------
    # Calling party transformation patterns
    # -------------------------------------------------------------------------

    def add_update_cnptx(self, pattern, partition, description, discard, prefix, plan, type, mask=''):
        """Create or update a calling party transformation pattern.

        :param pattern: dial-plan pattern string
        :param partition: name of the route partition the pattern belongs to
        :param description: human-readable description
        :param discard: digit discard instruction name (e.g. ``'PreDot'``)
        :param prefix: calling party prefix digits to prepend after transformation
        :param plan: calling party numbering plan (e.g. ``'ISDN'``)
        :param type: calling party number type (e.g. ``'National'``)
        :param mask: optional calling party transformation mask
        :return: UUID of the created or updated calling party transformation pattern
        """
        trans = {
            'pattern': pattern,
            'routePartitionName': partition,
            'description': description,
            'callingPartyTransformationMask': mask,
            'useCallingPartyPhoneMask': 'Off',
            'dialPlanName': '',
            'digitDiscardInstructionName': discard,
            'callingPartyPrefixDigits': prefix,
            'routeFilterName': '',
            'callingLinePresentationBit': 'Default',
            'callingPartyNumberingPlan': plan,
            'callingPartyNumberType': type,
            'mlppPreemptionDisabled': 'false',
        }
        try:
            p = self.service.getCallingPartyTransformationPattern(
                pattern=pattern, routePartitionName=partition, returnedTags={'pattern': '', 'routePartitionName': ''}
            )
        except zeep.exceptions.Fault:
            p = self.service.addCallingPartyTransformationPattern(callingPartyTransformationPattern=trans)
        else:
            p = self.service.updateCallingPartyTransformationPattern(**trans)
        return p['return']

    # -------------------------------------------------------------------------
    # Miscellaneous UCM objects
    # -------------------------------------------------------------------------

    def add_update_lrg(self, name, description):
        """Create or update a local route group (LRG).

        :param name: LRG name
        :param description: human-readable description
        :return: UUID of the created or updated LRG
        """
        try:
            r = self.service.getLocalRouteGroup(name=name)
        except zeep.exceptions.Fault as e:
            if e.message.startswith('Item not valid'):
                lrg = {'name': name, 'description': description}
                r = self.service.addLocalRouteGroup(localRouteGroup=lrg)
            else:
                raise
        else:
            lrg = {
                'uuid': r['return']['localRouteGroup']['uuid'],
                'name': name,
                'description': description,
                'newName': name,
                'newDescription': description,
            }
            r = self.service.updateLocalRouteGroup(localRouteGroup=lrg)
        return r['return']

    def add_update_route_list(self, **values):
        """Create or update a route list.

        :param values: AXL ``routeList`` field values; must include ``name``
        :return: UUID of the created or updated route list
        """
        try:
            r = self.service.getRouteList(name=values['name'])
        except zeep.exceptions.Fault:
            r = self.service.addRouteList(routeList=values)
        else:
            r = self.service.updateRouteList(**values)
        return r['return']

    def add_update_advertised_pattern(
        self, pattern, description, pattern_type, hosted_pstn_rule='No PSTN', pstn_strip=0, pstn_prepend=''
    ):
        """Create or update an advertised pattern (ILS/GDPR).

        :param pattern: E.164 pattern to advertise
        :param description: human-readable description
        :param pattern_type: pattern type string (e.g. ``'Enterprise Number'``)
        :param hosted_pstn_rule: hosted route PSTN rule (default ``'No PSTN'``)
        :param pstn_strip: number of leading digits to strip on PSTN failover
        :param pstn_prepend: digits to prepend on PSTN failover
        :return: UUID of the created or updated advertised pattern
        """
        ad_pattern = {
            'pattern': pattern,
            'description': description,
            'patternType': pattern_type,
            'hostedRoutePSTNRule': hosted_pstn_rule,
            'pstnFailStrip': pstn_strip,
            'pstnFailPrepend': pstn_prepend,
        }
        try:
            r = self.service.getAdvertisedPatterns(pattern=pattern)
        except zeep.exceptions.Fault:
            r = self.service.addAdvertisedPatterns(advertisedPatterns=ad_pattern)
        else:
            r = self.service.updateAdvertisedPatterns(**ad_pattern)
        return r['return']

    def add_update_date_time_group(self, dt_group):
        """Create or update a date/time group.

        :param dt_group: dict of AXL ``dateTimeGroup`` field values; must include ``name``
        :return: UUID of the created or updated date/time group
        """
        try:
            r = self.service.getDateTimeGroup(name=dt_group['name'])
        except zeep.exceptions.Fault:
            r = self.service.addDateTimeGroup(dateTimeGroup=dt_group)
        else:
            r = self.service.updateDateTimeGroup(**dt_group)
        return r['return']

    def add_update_device_pool(self, dp):
        """Create or update a device pool.

        :param dp: dict of AXL ``devicePool`` field values; must include ``name``
        :return: UUID of the created or updated device pool
        """
        try:
            r = self.service.getDevicePool(name=dp['name'])
        except zeep.exceptions.Fault:
            r = self.service.addDevicePool(devicePool=dp)
        else:
            r = self.service.updateDevicePool(**dp)
        return r['return']

    def add_update_line(self, dn):
        """Create or update a directory number (line).

        :param dn: dict of AXL ``line`` field values; must include ``pattern`` and
                   ``routePartitionName``
        :return: UUID of the created or updated line
        """
        try:
            r = self.service.getLine(pattern=dn['pattern'], routePartitionName=dn['routePartitionName'])
        except zeep.exceptions.Fault:
            r = self.service.addLine(line=dn)
        else:
            # 'usage' is not accepted by updateLine
            dn.pop('usage', None)
            r = self.service.updateLine(**dn)
        return r['return']

    def add_update_phone(self, phone):
        """Create or update a phone device.

        :param phone: dict of AXL ``phone`` field values; must include ``name``
        :return: UUID of the created or updated phone
        """
        try:
            r = self.service.getPhone(name=phone['name'])
        except zeep.exceptions.Fault:
            r = self.service.addPhone(phone=phone)
        else:
            # These read-only fields are rejected by updatePhone
            to_pop = ['product', 'protocolSide', 'class', 'protocol']
            for p in to_pop:
                phone.pop(p, None)
            r = self.service.updatePhone(**phone)
        return r['return']

    def add_update_fgt(self, fgt):
        """Create or update a feature group template.

        :param fgt: dict of AXL ``featureGroupTemplate`` field values; must include ``name``
        :return: UUID of the created or updated feature group template
        """
        try:
            r = self.service.getFeatureGroupTemplate(name=fgt['name'])
        except zeep.exceptions.Fault:
            r = self.service.addFeatureGroupTemplate(featureGroupTemplate=fgt)
        else:
            r = self.service.updateFeatureGroupTemplate(**fgt)
        return r['return']

    def add_update_ldap_filter(self, filter):
        """Create or update an LDAP filter.

        :param filter: dict of AXL ``ldapFilter`` field values; must include ``name``
        :return: UUID of the created or updated LDAP filter
        """
        try:
            r = self.service.getLdapFilter(name=filter['name'])
        except zeep.exceptions.Fault:
            r = self.service.addLdapFilter(ldapFilter=filter)
        else:
            r = self.service.updateLdapFilter(**filter)
        return r['return']

    def add_update_ldap_directory(self, directory):
        """Create or update an LDAP directory configuration.

        :param directory: dict of AXL ``ldapDirectory`` field values; must include ``name``
        :return: UUID of the created or updated LDAP directory
        """
        try:
            r = self.service.getLdapDirectory(name=directory['name'])
        except zeep.exceptions.Fault:
            r = self.service.addLdapDirectory(ldapDirectory=directory)
        else:
            # These fields are not accepted by updateLdapDirectory
            attrs_not_in_update = ['mailId', 'directoryUri', 'middleName', 'phoneNumber']
            for a in attrs_not_in_update:
                directory.pop(a, None)
            r = self.service.updateLdapDirectory(**directory)
        return r['return']

    def add_update_sip_trunk_security_profile(self, profile):
        """Create or update a SIP trunk security profile, merging caller values over defaults.

        :param profile: dict of SIP trunk security profile fields; must include ``name``
        :return: UUID of the created or updated SIP trunk security profile
        """
        default_security_profile = {
            'securityMode': 'Non Secure',
            'incomingTransport': 'TCP+UDP',
            'outgoingTransport': 'TCP',
            'digestAuthentication': 'false',
            'noncePolicyTime': '600',
            'x509SubjectName': '',
            'incomingPort': '5060',
            'applLevelAuthentication': 'false',
            'acceptPresenceSubscription': 'false',
            'acceptOutOfDialogRefer': 'false',
            'acceptUnsolicitedNotification': 'false',
            'allowReplaceHeader': 'false',
            'transmitSecurityStatus': 'false',
            'sipV150OutboundSdpOfferFiltering': 'Use Default Filter',
            'allowChargingHeader': 'false',
        }
        # Merge caller-supplied values over the defaults
        security_profile = dict(default_security_profile)
        security_profile.update(**profile)
        profile = security_profile
        try:
            r = self.service.getSipTrunkSecurityProfile(name=profile['name'])
        except zeep.exceptions.Fault:
            r = self.service.addSipTrunkSecurityProfile(sipTrunkSecurityProfile=profile)
        else:
            r = self.service.updateSipTrunkSecurityProfile(**profile)
        return r['return']

    def add_update_sip_trunk(self, trunk):
        """Create or update a SIP trunk, merging caller values over standard defaults.

        :param trunk: dict of SIP trunk field values; must include ``name``
        :return: UUID of the created or updated SIP trunk
        """
        default_sip_trunk = {
            'product': 'SIP Trunk',
            'class': 'Trunk',
            'protocol': 'SIP',
            'protocolSide': 'Network',
            'callingSearchSpaceName': '',
            'devicePoolName': 'Default',
            'commonDeviceConfigName': '',
            'networkLocation': 'Use System Default',
            'locationName': 'Hub_None',
            'mediaResourceListName': '',
            'networkHoldMohAudioSourceId': '',
            'userHoldMohAudioSourceId': '',
            'automatedAlternateRoutingCssName': '',
            'aarNeighborhoodName': '',
            'packetCaptureMode': 'None',
            'packetCaptureDuration': '0',
            'loadInformation': '',
            'traceFlag': 'false',
            'mlppIndicationStatus': 'Off',
            'preemption': 'Disabled',
            'useTrustedRelayPoint': 'Default',
            'retryVideoCallAsAudio': 'true',
            'securityProfileName': 'Non Secure SIP Trunk Profile',
            'sipProfileName': 'Standard SIP Profile',
            'cgpnTransformationCssName': '',
            'useDevicePoolCgpnTransformCss': 'true',
            'geoLocationName': '',
            'geoLocationFilterName': '',
            'sendGeoLocation': 'false',
            'cdpnTransformationCssName': '',
            'useDevicePoolCdpnTransformCss': 'true',
            'unattendedPort': 'false',
            'transmitUtf8': 'false',
            'subscribeCallingSearchSpaceName': '',
            'rerouteCallingSearchSpaceName': '',
            'referCallingSearchSpaceName': '',
            'mtpRequired': 'false',
            'presenceGroupName': 'Standard Presence group',
            'unknownPrefix': 'Default',
            'tkSipCodec': '711ulaw',
            'connectedNamePresentation': 'Default',
            'connectedPartyIdPresentation': 'Default',
            'callingPartySelection': 'Originator',
            'callingname': 'Default',
            'callingLineIdPresentation': 'Default',
            'prefixDn': '',
            'callerName': '',
            'callerIdDn': '',
            'acceptInboundRdnis': 'true',
            'acceptOutboundRdnis': 'true',
            'srtpAllowed': 'false',
            'srtpFallbackAllowed': 'true',
            'isPaiEnabled': 'true',
            'sipPrivacy': 'Default',
            'isRpidEnabled': 'true',
            'sipAssertedType': 'Default',
            'dtmfSignalingMethod': 'No Preference',
            'routeClassSignalling': 'Default',
            'sipTrunkType': 'None(Default)',
            'pstnAccess': 'false',
            'imeE164TransformationName': '',
            'useImePublicIpPort': 'false',
            'useDevicePoolCntdPnTransformationCss': 'true',
            'useDevicePoolCgpnTransformCssUnkn': 'true',
            'sipNormalizationScriptName': '',
            'runOnEveryNode': 'true',
        }
        # Merge caller-supplied values over the defaults
        sip_trunk = dict(default_sip_trunk)
        sip_trunk.update(**trunk)
        trunk = sip_trunk
        try:
            r = self.service.getSipTrunk(name=trunk['name'])
        except zeep.exceptions.Fault:
            r = self.service.addSipTrunk(sipTrunk=trunk)
        else:
            # These read-only or create-only fields are rejected by updateSipTrunk
            attrs_not_in_update = ['product', 'protocolSide', 'loadInformation', 'protocol', 'traceFlag', 'class']
            for a in attrs_not_in_update:
                trunk.pop(a, None)
            r = self.service.updateSipTrunk(**trunk)

        return r['return']

    def add_update_route_group(self, route_group):
        """Create or update a route group.

        :param route_group: dict of AXL ``routeGroup`` field values; must include ``name``
        :return: UUID of the created or updated route group
        """
        try:
            r = self.service.getRouteGroup(name=route_group['name'])
        except zeep.exceptions.Fault:
            r = self.service.addRouteGroup(routeGroup=route_group)
        else:
            r = self.service.updateRouteGroup(**route_group)
        return r['return']

    def add_update_sip_route_pattern(self, route_pattern):
        """Create or update a SIP route pattern.

        :param route_pattern: dict of AXL ``sipRoutePattern`` field values; must include ``pattern``
                              and ``routePartitionName``
        :return: UUID of the created or updated SIP route pattern
        """
        try:
            r = self.service.getSipRoutePattern(
                pattern=route_pattern['pattern'], routePartitionName=route_pattern['routePartitionName']
            )
        except zeep.exceptions.Fault:
            r = self.service.addSipRoutePattern(sipRoutePattern=route_pattern)
        else:
            # 'usage' is not accepted by updateSipRoutePattern
            attrs_not_in_update = ['usage']
            for a in attrs_not_in_update:
                route_pattern.pop(a, None)
            r = self.service.updateSipRoutePattern(**route_pattern)
        return r['return']

    def add_update_universal_device_template(self, site_udt):
        """Create or update a universal device template, merging caller values over defaults.

        :param site_udt: dict of universal device template fields; must include ``name``
        :return: UUID of the created or updated universal device template
        """
        base_udt = {
            'deviceSecurityProfile': 'Universal Device Template - Model-independent Security Profile',
            'phoneButtonTemplate': 'Universal Device Template Button Layout',
            'sipDialRules': None,
            'callingPartyTransformationCSSForInboundCalls': None,
            'callingPartyTransformationCSSForOutboundCalls': None,
            'reroutingCallingSearchSpace': None,
            'subscribeCallingSearchSpaceName': None,
            'useDevicePoolCallingPartyTransformationCSSforInboundCalls': True,
            'useDevicePoolCallingPartyTransformationCSSforOutboundCalls': True,
            'commonPhoneProfile': 'Standard Common Phone Profile',
            'commonDeviceConfiguration': None,
            'softkeyTemplate': 'Cisco User with Feature Hardkeys',
            'featureControlPolicy': None,
            'phonePersonalization': 'Default',
            'mtpPreferredOriginatingCodec': '711ulaw',
            'outboundCallRollover': 'No Rollover',
            'mediaTerminationPointRequired': False,
            'unattendedPort': False,
            'requiredDtmfReception': False,
            'rfc2833Disabled': False,
            'useTrustedRelayPoint': 'Default',
            'protectedDevice': False,
            'servicesProvisioning': 'Default',
            'packetCaptureMode': 'None',
            'packetCaptureDuration': 0,
            'secureShellUser': None,
            'secureShellPassword': None,
            'userLocale': None,
            'networkLocale': None,
            'mlppDomain': None,
            'mlppIndication': 'Default',
            'mlppPreemption': 'Default',
            'dndOption': 'Use Common Phone Profile Setting',
            'dndIncomingCallAlert': None,
            'aarGroup': 'Default',
            'blfPresenceGroup': 'Standard Presence group',
            'blfAudibleAlertSettingPhoneBusy': 'Default',
            'blfAudibleAlertSettingPhoneIdle': 'Default',
            'userHoldMohAudioSource': None,
            'networkHoldMohAudioSource': None,
            'geoLocation': None,
            'deviceMobilityMode': 'Default',
            'mediaResourceGroupList': None,
            'remoteDevice': False,
            'hotlineDevice': False,
            'retryVideoCallAsAudio': True,
            'requireOffPremiseLocation': False,
            'ownerUserId': None,
            'mobilityUserId': None,
            'joinAcrossLines': 'Default',
            'alwaysUsePrimeLine': 'Default',
            'alwaysUsePrimeLineForVoiceMessage': 'Default',
            'singleButtonBarge': 'Default',
            'builtInBridge': 'Default',
            'allowControlOfDeviceFromCti': True,
            'ignorePresentationIndicators': False,
            'enableExtensionMobility': True,
            'servicesUrl': None,
        }
        # Merge caller-supplied values over the defaults
        udt = dict(base_udt)
        udt.update(**site_udt)
        try:
            r = self.service.getUniversalDeviceTemplate(name=udt['name'])
        except zeep.exceptions.Fault:
            r = self.service.addUniversalDeviceTemplate(universalDeviceTemplate=udt)
        else:
            r = self.service.updateUniversalDeviceTemplate(**udt)
        return r['return']

    def add_update_universal_line_template(self, site_ult):
        """Create or update a universal line template, merging caller values over defaults.

        :param site_ult: dict of universal line template fields; must include ``name``
        :return: UUID of the created or updated universal line template
        """
        base_ult = {
            'voiceMailProfile': None,
            'extCallControlProfile': None,
            'blfPresenceGroup': 'Standard Presence group',
            'partyEntranceTone': 'Default',
            'autoAnswer': 'Auto Answer Off',
            'rejectAnonymousCall': True,
            'userHoldMohAudioSource': None,
            'networkHoldMohAudioSource': None,
            'retainDestInCallFwdHistory': False,
            'CssActivationPolicy': 'Use System Default',
            'fwdDestExtCallsWhenNotRetrieved': None,
            'cssFwdExtCallsWhenNotRetrieved': None,
            'fwdDestInternalCallsWhenNotRetrieved': None,
            'cssFwdInternalCallsWhenNotRetrieved': None,
            'parkMonitorReversionTime': 60,
            'target': None,
            'mlppCss': None,
            'mlppNoAnsRingDuration': None,
            'holdReversionRingDuration': None,
            'holdReversionNotificationInterval': None,
            'busyIntCallsDestination': None,
            'busyExtCallsDestination': None,
            'noAnsIntCallsDestination': None,
            'noAnsExtCallsDestination': None,
            'noCoverageIntCallsDestination': None,
            'noCoverageExtCallsDestination': None,
            'unregisteredIntCallsDestination': None,
            'unregisteredExtCallsDestination': None,
            'ctiFailureDestination': None,
        }
        # Merge caller-supplied values over the defaults
        ult = dict(base_ult)
        ult.update(**site_ult)
        try:
            r = self.service.getUniversalLineTemplate(name=ult['name'])
        except zeep.exceptions.Fault:
            r = self.service.addUniversalLineTemplate(universalLineTemplate=ult)
        else:
            r = self.service.updateUniversalLineTemplate(**ult)
        return r['return']

    def add_update_user_profile_provision(self, upp):
        """Create or update a user profile provisioning record.

        :param upp: dict of AXL ``userProfileProvision`` field values; must include ``name``
        :return: UUID of the created or updated user profile provisioning record
        """
        try:
            r = self.service.getUserProfileProvision(name=upp['name'])
        except zeep.exceptions.Fault:
            r = self.service.addUserProfileProvision(userProfileProvision=upp)
        else:
            r = self.service.updateUserProfileProvision(**upp)
        return r['return']

    def add_update_cti_rp(self, cti_rp):
        """Create or update a CTI route point.

        :param cti_rp: dict of AXL ``ctiRoutePoint`` field values; must include ``name``
        :return: UUID of the created or updated CTI route point
        """
        try:
            r = self.service.getCtiRoutePoint(name=cti_rp['name'])
        except zeep.exceptions.Fault:
            r = self.service.addCtiRoutePoint(ctiRoutePoint=cti_rp)
        else:
            # These read-only fields are rejected by updateCtiRoutePoint
            to_pop = ['product', 'protocolSide', 'class', 'protocol']
            for p in to_pop:
                cti_rp.pop(p, None)
            r = self.service.updateCtiRoutePoint(**cti_rp)
        return r['return']

    def add_update_app_user(self, app_user):
        """Create or update an application user.

        :param app_user: dict of AXL ``appUser`` field values; must include ``userid``
        :return: UUID of the created or updated application user
        """
        try:
            r = self.service.getAppUser(userid=app_user['userid'])
        except zeep.exceptions.Fault:
            r = self.service.addAppUser(appUser=app_user)
        else:
            r = self.service.updateAppUser(**app_user)
        return r['return']

    def add_update_phone_button_template(self, pbt):
        """Create or update a phone button template.

        :param pbt: dict of AXL ``phoneButtonTemplate`` field values; must include ``name``
        :return: UUID of the created or updated phone button template
        """
        try:
            r = self.service.getPhoneButtonTemplate(name=pbt['name'])
        except zeep.exceptions.Fault:
            r = self.service.addPhoneButtonTemplate(phoneButtonTemplate=pbt)
        else:
            # 'basePhoneTemplateName' is a create-only field not accepted by updatePhoneButtonTemplate
            pbt.pop('basePhoneTemplateName', None)
            r = self.service.updatePhoneButtonTemplate(**pbt)
        return r['return']
