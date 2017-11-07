"""
Contains helpers to assist in "migrations" from one version of
PynamoDB to the next, in cases where breaking changes have happened.
"""

import logging

from botocore.exceptions import ClientError
from pynamodb.exceptions import UpdateError
from pynamodb.expressions.operand import Path

log = logging.getLogger(__name__)


def _build_lba_filter_condition(attribute_names):
    """
    Build a filter condition suitable for passing to scan/rate_limited_scan, which
    will filter out any items for which none of the given attributes have native
    DynamoDB type of 'N'.
    """
    int_filter_condition = None
    for attr_name in attribute_names:
        if int_filter_condition is None:
            int_filter_condition = Path(attr_name).is_type('N')
        else:
            int_filter_condition |= Path(attr_name).is_type('N')

    return int_filter_condition


def migrate_boolean_attributes(model_class,
                               attribute_names,
                               read_capacity_to_consume_per_second=10,
                               allow_rate_limited_scan_without_consumed_capacity=False,
                               mock_conditional_update_failure=False):
    """
    Migrates boolean attributes per GitHub issue 404.

    For context, see https://github.com/pynamodb/PynamoDB/issues/404

    Will scan through all objects and perform a conditional update
    against any items that store any of the given attribute names as
    integers. Rate limiting is performed by passing an appropriate
    value as `read_capacity_to_consume_per_second` (which defaults to
    something extremely conservative and slow).

    Note that updates require provisioned write capacity as
    well. Please see
    http://docs.aws.amazon.com/amazondynamodb/latest/developerguide/HowItWorks.ProvisionedThroughput.html
    for more information. Keep in mind that there is not a simple 1:1
    mapping between provisioned read capacity and write capacity. Make
    sure they are balanced. A conservative calculation would assume
    that every object visted results in an update.

    The function with log at level `INFO` the final outcome, and the
    return values help identify how many items needed changing and how
    many of them succeed. For example, if you had 10 items in the
    table and every one of them had an attribute that needed
    migration, and upon migration we had one item which failed the
    migration due to a concurrent update by another writer, the return
    value would be:

      `(10, 1)`

    Suggesting that 9 were updated successfully.

    It is suggested that the migration step be re-ran until the return
    value is `(0, 0)`.

    :param model_class: The Model class for which you are migrating. This should
                        be the up-to-date Model class using a BooleanAttribute for
                        the relevant attributes.
    :param attribute_names: List of strings that signifiy the names of attributes which
                            are potentially in need of migration.
    :param read_capacity_to_consume_per_second: Passed along to the underlying
                                                `rate_limited_scan` and intended as
                                                the mechanism to rate limit progress. Please
                                                see notes below around write capacity.
    :param allow_rate_limited_scan_without_consumed_capacity: Passed along to `rate_limited_scan`; intended
                                                              to allow unit tests to pass against DynamoDB Local.
    :param mock_conditional_update_failure: Only used for unit testing. When True, the conditional update expression
                                            used internally is updated such that it is guaranteed to fail. This is
                                            meant to trigger the code path in boto, to allow us to unit test that
                                            we are jumping through appropriate hoops handling the resulting
                                            failure and distinguishing it from other failures.

    :return: (number_of_items_in_need_of_update, number_of_them_that_failed_due_to_conditional_update)
    """
    log.info('migrating items; no progress will be reported until completed; this may take a while')
    num_items_with_actions = 0
    num_update_failures = 0

    for item in model_class.rate_limited_scan(_build_lba_filter_condition(attribute_names),
                                              read_capacity_to_consume_per_second=read_capacity_to_consume_per_second,
                                              allow_rate_limited_scan_without_consumed_capacity=allow_rate_limited_scan_without_consumed_capacity):
        actions = []
        condition = None
        for attr_name in attribute_names:
            if not hasattr(item, attr_name):
                raise ValueError('attribute {0} does not exist on model'.format(attr_name))
            old_value = getattr(item, attr_name)
            if old_value is None:
                continue
            if not isinstance(old_value, bool):
                raise ValueError('attribute {0} does not appear to be a boolean attribute'.format(attr_name))

            actions.append(getattr(model_class, attr_name).set(getattr(item, attr_name)))

            if condition is None:
                condition = Path(attr_name) == (1 if old_value else 0)
            else:
                condition = condition & Path(attr_name) == (1 if old_value else 0)

        if actions:
            if mock_conditional_update_failure:
                condition = condition & (Path('__bogus_mock_attribute') == 5)
            try:
                num_items_with_actions += 1
                item.update(actions=actions, condition=condition)
            except UpdateError as e:
                if isinstance(e.cause, ClientError):
                    code = e.cause.response['Error'].get('Code')
                    if code == 'ConditionalCheckFailedException':
                        log.warn('conditional update failed (concurrent writes?) for object: %s (you will need to re-run migration)', item)
                        num_update_failures += 1
                    else:
                        raise
                else:
                    raise
    log.info('finished migrating; %s items required updates, %s failed due to racing writes and require re-running migration',
             num_items_with_actions, num_update_failures)
    return num_items_with_actions, num_update_failures
