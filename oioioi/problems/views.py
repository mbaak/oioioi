# coding: utf-8
import os
import shutil
import tempfile
from functools import wraps
from itertools import groupby
from operator import attrgetter

import six.moves.urllib.parse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.files.base import File
from django.db import transaction
from django.db.models import Case, CharField, F, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.safestring import mark_safe
from django.utils.translation import get_language
from django.utils.translation import ugettext_lazy as _
from oioioi.base.permissions import enforce_condition, not_anonymous
from oioioi.base.utils import jsonify, tabbed_view
from oioioi.base.utils.archive import Archive
from oioioi.base.utils.redirect import safe_redirect
from oioioi.contests.controllers import submission_template_context
from oioioi.contests.current_contest import ContestMode
from oioioi.contests.models import (
    ProblemInstance,
    Submission,
    SubmissionReport,
    UserResultForProblem,
)
from oioioi.contests.utils import administered_contests, is_contest_basicadmin
from oioioi.filetracker.utils import stream_file
from oioioi.problems.forms import ProblemsetSourceForm
from oioioi.problems.menu import navbar_links_registry
from oioioi.problems.models import (
    AlgorithmTag,
    AlgorithmTagLocalization,
    AlgorithmTagProposal,
    DifficultyProposal,
    DifficultyTag,
    OriginInfoCategory,
    OriginInfoValue,
    OriginInfoValueLocalization,
    OriginTag,
    OriginTagLocalization,
    Problem,
    ProblemAttachment,
    ProblemPackage,
    ProblemStatement,
    Tag,
    UserStatistics,
)

# problem_site_statement_zip_view is used in one of the tabs
# in problem_site.py. We placed the view in problem_site.py
# instead of views.py to avoid circular imports. We still import
# it here to use it in urls.py.
from oioioi.problems.problem_site import (
    problem_site_statement_zip_view,
    problem_site_tab_registry,
)
from oioioi.problems.problem_sources import problem_sources
from oioioi.problems.utils import (
    can_add_to_problemset,
    can_admin_instance_of_problem,
    can_admin_problem,
    can_admin_problem_instance,
    generate_add_to_contest_metadata,
    generate_model_solutions_context,
    get_prefetched_value,
    query_statement,
    show_proposal_form,
)
from oioioi.programs.models import ModelSolution
from unidecode import unidecode

if settings.CONTEST_MODE == ContestMode.neutral:
    navbar_links_registry.register(
        name='contests_list',
        text=_('Contests'),
        url_generator=lambda request: reverse('select_contest'),
        order=100,
    )

navbar_links_registry.register(
    name='problemset',
    text=_('Problemset'),
    url_generator=lambda request: reverse('problemset_main'),
    order=200,
)

navbar_links_registry.register(
    name='task_archive',
    text=_('Task archive'),
    url_generator=lambda request: reverse('task_archive'),
    order=300,
)


def show_statement_view(request, statement_id):
    statement = get_object_or_404(ProblemStatement, id=statement_id)
    if not can_admin_instance_of_problem(request, statement.problem):
        raise PermissionDenied
    return stream_file(statement.content, statement.download_name)


def show_problem_attachment_view(request, attachment_id):
    attachment = get_object_or_404(ProblemAttachment, id=attachment_id)
    if not can_admin_instance_of_problem(request, attachment.problem):
        raise PermissionDenied
    return stream_file(attachment.content, attachment.download_name)


def _get_package(request, package_id, contest_perm=None):
    package = get_object_or_404(ProblemPackage, id=package_id)
    has_perm = False
    if package.contest:
        has_perm = request.user.has_perm(contest_perm, package.contest) or (
            contest_perm == 'contests.contest_basicadmin'
            and request.user.has_perm('contests.contest_admin', package.contest)
        )
    elif package.problem:
        has_perm = can_admin_problem(request, package.problem)
    else:
        has_perm = request.user.is_superuser
    if not has_perm:
        raise PermissionDenied
    return package


def download_problem_package_view(request, package_id):
    package = _get_package(request, package_id, 'contests.contest_admin')
    return stream_file(package.package_file, package.download_name)


def download_package_traceback_view(request, package_id):
    package = _get_package(request, package_id, 'contests.contest_basicadmin')
    if not package.traceback:
        raise Http404
    return stream_file(
        package.traceback,
        'package_%s_%d_traceback.txt' % (package.problem_name, package.id),
    )


def add_or_update_problem(request, contest, template):
    if 'problem' in request.GET:
        existing_problem = get_object_or_404(Problem, id=request.GET['problem'])
        if (
            contest
            and not existing_problem.probleminstance_set.filter(
                contest=contest
            ).exists()
        ):
            raise Http404
        if not can_admin_problem(request, existing_problem):
            raise PermissionDenied
    else:
        existing_problem = None
        if not request.user.has_perm('problems.problems_db_admin'):
            if contest and (not is_contest_basicadmin(request)):
                raise PermissionDenied

    navbar_links = navbar_links_registry.template_context(request)
    problemset_tabs = generate_problemset_tabs(request)

    context = {
        'existing_problem': existing_problem,
        'navbar_links': navbar_links,
        'problemset_tabs': problemset_tabs,
    }
    tab_kwargs = {'contest': contest, 'existing_problem': existing_problem}

    tab_link_params = request.GET.dict()

    def build_link(tab):
        tab_link_params['key'] = tab.key
        return request.path + '?' + six.moves.urllib.parse.urlencode(tab_link_params)

    return tabbed_view(
        request,
        template,
        context,
        problem_sources(request, existing_problem=existing_problem is not None),
        tab_kwargs,
        build_link,
    )


@transaction.non_atomic_requests
def add_or_update_problem_view(request):
    return add_or_update_problem(
        request, request.contest, 'problems/add-or-update.html'
    )


def filter_problems_by_origin(problems, origintags):
    """The filters are almost always logical ANDed, the only exception to
    this are OriginInfoValues within their OriginInfoCategory, which are
    logical ORred - it is possible to search for example for tasks from
    round "1 or 2" and year "2011 or 2012 or 2013".
    Searching in Problemset from the Task Archive relies on this behaviour.
    """
    info = {}
    for tag in origintags:
        tag = tag.split('_')

        if len(tag) in (1, 2):
            if not OriginTag.objects.filter(name=tag[0]).exists():
                raise Http404

            if tag[0] not in info:
                info[tag[0]] = {}

            if len(tag) == 2:
                value = OriginInfoValue.objects.filter(
                    parent_tag__name=tag[0], value=tag[1]
                )
                if not value.exists():
                    raise Http404
                value = value.get()

                category = value.category.name
                if category not in info[tag[0]]:
                    # pk=None doesn't match any problem, needed for logical OR
                    info[tag[0]][category] = Q(pk=None)
                info[tag[0]][category] |= Q(origininfovalue__value=value.value)
        else:
            raise Http404

    for tag, categories in info.items():
        problems = problems.filter(origintag__name=tag)
        for category, q in categories.items():
            print(q)
            problems = problems.filter(q)

    return problems


def search_problems_in_problemset(datadict):
    query = unidecode(six.text_type(datadict.get('q', '')))
    tags = datadict.getlist('tag')
    algorithmtags = datadict.getlist('algorithm')
    difficultytags = datadict.getlist('difficulty')
    origintags = datadict.getlist('origin')

    problems = Problem.objects.all()

    if query:
        problems = problems.filter(
            Q(ascii_name__icontains=query) | Q(short_name__icontains=query)
        )
    if tags:  # Old tags, deprecated
        problems = problems.filter(tag__name__in=tags)
    if algorithmtags:
        problems = problems.filter(algorithmtag__name__in=algorithmtags)
    if difficultytags:
        problems = problems.filter(difficultytag__name__in=difficultytags)
    if origintags:
        problems = filter_problems_by_origin(problems, origintags)

    return problems


def generate_problemset_tabs(request):
    tabs = [{'name': _('Public problems'), 'url': reverse('problemset_main')}]

    if request.user.is_authenticated:
        tabs.append(
            {'name': _('My problems'), 'url': reverse('problemset_my_problems')}
        )

        if 'oioioi.problemsharing' in settings.INSTALLED_APPS:
            if request.user.has_perm('teachers.teacher'):
                tabs.append(
                    {
                        'name': _('Shared with me'),
                        'url': reverse('problemset_shared_with_me'),
                    }
                )

        if request.user.is_superuser:
            tabs.append(
                {'name': _('All problems'), 'url': reverse('problemset_all_problems')}
            )
        if can_add_to_problemset(request):
            tabs.append(
                {
                    'name': _('Add problem'),
                    'url': reverse('problemset_add_or_update'),
                    'color': "-green",
                }
            )

    return tabs


def problemset_get_problems(request):
    problems = search_problems_in_problemset(request.GET)

    if settings.PROBLEM_STATISTICS_AVAILABLE:
        # We annotate all of the statistics to assure that the display
        # will be correct from the logic's point of view.
        problems = problems.select_related('statistics').annotate(
            statistics_submitted=Case(
                When(statistics__isnull=True, then=0),
                default=F('statistics__submitted'),
            ),
            statistics_solved_pc=Case(
                When(statistics__submitted=0, then=None),
                default=100 * F('statistics__solved') / F('statistics__submitted'),
            ),
            statistics_avg_best_score=Case(
                When(statistics__isnull=True, then=None),
                When(statistics__submitted=0, then=None),
                default=F('statistics__avg_best_score'),
            ),
        )

        if request.user.is_authenticated:
            user_statistics = UserStatistics.objects.filter(
                problem_statistics=OuterRef('statistics__pk'), user=request.user
            )

            problems = problems.annotate(
                # If there are no official submissions (i.e. all of the user's
                # submissions to this problem are ignored), treat the result
                # as if it didn't exist at all.
                user_statistics_user_score=Subquery(
                    user_statistics.annotate(
                        visible_score=(
                            Case(
                                When(has_submitted=False, then=None),
                                default=F('best_score'),
                            )
                        )
                    ).values('visible_score')[:1]
                ),
                user_statistics_has_submitted=Coalesce(
                    Subquery(user_statistics.values('has_submitted')[:1]), False
                ),
                user_statistics_has_solved=Coalesce(
                    Subquery(user_statistics.values('has_solved')[:1]), False
                ),
            ).annotate(
                score_display_type=Case(
                    When(user_statistics_has_solved=True, then=Value("OK")),  # solved
                    When(
                        user_statistics_user_score__gt=0, then=Value("TRIED")
                    ),  # score is positive
                    When(
                        user_statistics_user_score__isnull=False, then=Value("FAILED")
                    ),  # score: 0
                    output_field=CharField(),
                )
            )

            filter_types = {
                'solved': Q(user_statistics_has_solved=True),
                'attempted': Q(user_statistics_has_solved=False)
                & Q(user_statistics_has_submitted=True),
                'not_attempted': Q(user_statistics_has_submitted=False),
            }
            if 'filter' in request.GET:
                type = request.GET['filter']

                if type == 'all':
                    pass
                elif type in filter_types:
                    problems = problems.filter(filter_types[type])
                else:
                    raise Http404

    order_fields = ('name', 'short_name')
    order_statistics = ('submitted', 'solved_pc', 'avg_best_score')
    order_user_statistics = 'user_score'
    if 'order_by' in request.GET:
        field = request.GET['order_by']

        if field in order_fields:
            lookup = F(field)
        elif field in order_statistics:
            lookup = F('statistics_' + field)
        elif field in order_user_statistics:
            lookup = F('user_statistics_' + field)
        else:
            raise Http404

        if 'desc' in request.GET:
            problems = problems.order_by(lookup.desc(nulls_last=True))
        else:
            problems = problems.order_by(lookup.asc(nulls_first=True))
    else:
        problems = problems.order_by('ascii_name')

    problems = problems.select_related('problemsite')

    return problems


def problemset_generate_view(request, page_title, problems, view_type):
    # We want to show "Add to contest" button only
    # if user is contest admin for any contest.
    show_add_button, administered_recent_contests = generate_add_to_contest_metadata(
        request
    )
    show_tags = settings.PROBLEM_TAGS_VISIBLE
    show_statistics = settings.PROBLEM_STATISTICS_AVAILABLE
    show_user_statistics = show_statistics and request.user.is_authenticated
    show_filters = (
        settings.PROBLEM_STATISTICS_AVAILABLE and request.user.is_authenticated
    )
    col_proportions = {
        'id': 1,
        'name': 2,
        'tags': 5,
        'statistics1': 1,
        'statistics2': 1,
        'statistics3': 1,
        'user_score': 1,
        'add_button': 1,
    }
    if not show_add_button:
        col_proportions['tags'] += col_proportions.pop('add_button')
    if not show_statistics:
        col_proportions['id'] += col_proportions.pop('statistics1')
        col_proportions['name'] += col_proportions.pop('statistics2')
        col_proportions['tags'] += col_proportions.pop('statistics3')
    if not show_user_statistics:
        col_proportions['tags'] += col_proportions.pop('user_score')
    if not show_tags:
        col_proportions['name'] += col_proportions.pop('tags')
    assert sum(col_proportions.values()) == 13
    form = ProblemsetSourceForm("")

    navbar_links = navbar_links_registry.template_context(request)
    problemset_tabs = generate_problemset_tabs(request)

    origintags = {}
    for param in request.GET.getlist('origin'):
        param = param.split('_')
        if len(param) in (1, 2):
            if param[0] not in origintags:
                origintags[param[0]] = []
            if len(param) == 2:
                origintags[param[0]].append(param[1])
        else:
            raise Http404

    return TemplateResponse(
        request,
        'problems/problemset/problem-list.html',
        {
            'problems': problems,
            'navbar_links': navbar_links,
            'problemset_tabs': problemset_tabs,
            'page_title': page_title,
            'select_problem_src': request.GET.get('select_problem_src'),
            'problem_search': request.GET.get('q', ''),
            'tags': request.GET.getlist('tag'),
            'origintags': origintags,
            'algorithmtags': request.GET.getlist('algorithm'),
            'difficultytags': request.GET.getlist('difficulty'),
            'show_tags': show_tags,
            'show_statistics': show_statistics,
            'show_user_statistics': show_user_statistics,
            'show_filters': show_filters,
            'show_search_bar': True,
            'show_add_button': show_add_button,
            'administered_recent_contests': administered_recent_contests,
            'col_proportions': col_proportions,
            'form': form,
            'view_type': view_type,
        },
    )


def problemset_main_view(request):
    page_title = _("Welcome to problemset, the place where all the problems are.")
    problems_pool = problemset_get_problems(request)
    problems = problems_pool.filter(
        visibility=Problem.VISIBILITY_PUBLIC, problemsite__isnull=False
    )
    return problemset_generate_view(request, page_title, problems, "public")


def problemset_my_problems_view(request):
    page_title = _("My problems")
    problems_pool = problemset_get_problems(request)
    problems = problems_pool.filter(author=request.user, problemsite__isnull=False)
    return problemset_generate_view(request, page_title, problems, "my")


def problemset_shared_with_me_view(request):
    from oioioi.problemsharing.models import Friendship

    page_title = _("Shared with me")
    problems_pool = problemset_get_problems(request)
    friends = Friendship.objects.filter(receiver=request.user).values_list(
        'creator', flat=True
    )
    problems = problems_pool.filter(
        visibility=Problem.VISIBILITY_FRIENDS,
        author__in=friends,
        problemsite__isnull=False,
    )
    return problemset_generate_view(request, page_title, problems, "my")


def problemset_all_problems_view(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    page_title = _("All problems")
    problems_pool = problemset_get_problems(request)
    problems = problems_pool.filter(problemsite__isnull=False)
    return problemset_generate_view(request, page_title, problems, "all")


def problem_site_view(request, site_key):
    problem = get_object_or_404(Problem, problemsite__url_key=site_key)
    package = ProblemPackage.objects.filter(problem=problem).first()
    show_add_button, administered_recent_contests = generate_add_to_contest_metadata(
        request
    )
    extra_actions = problem.controller.get_extra_problem_site_actions(problem)
    navbar_links = navbar_links_registry.template_context(request)
    problemset_tabs = generate_problemset_tabs(request)
    problemset_tabs.append(
        {
            'name': _('Problem view'),
            'url': reverse('problem_site', kwargs={'site_key': site_key}),
        }
    )
    context = {
        'problem': problem,
        'problemsite_key': site_key,
        'package': package if package and package.package_file else None,
        'extra_actions': extra_actions,
        'can_admin_problem': can_admin_problem(request, problem),
        'select_problem_src': request.GET.get('select_problem_src'),
        'show_add_button': show_add_button,
        'show_proposal_form': show_proposal_form(problem, request.user),
        'administered_recent_contests': administered_recent_contests,
        'navbar_links': navbar_links,
        'problemset_tabs': problemset_tabs,
    }
    tab_kwargs = {'problem': problem}

    tab_link_params = request.GET.dict()
    if 'page' in tab_link_params:
        del tab_link_params['page']

    def build_link(tab):
        tab_link_params['key'] = tab.key
        return request.path + '?' + six.moves.urllib.parse.urlencode(tab_link_params)

    return tabbed_view(
        request,
        'problems/problemset/problem-site.html',
        context,
        problem_site_tab_registry,
        tab_kwargs,
        build_link,
    )


def problem_site_external_statement_view(request, site_key):
    problem = get_object_or_404(Problem, problemsite__url_key=site_key)
    statement = query_statement(problem.id)
    if not statement:
        raise Http404
    if statement.extension == '.zip' and not can_admin_problem(request, problem):
        raise PermissionDenied
    return stream_file(statement.content, statement.download_name)


def problem_site_external_attachment_view(request, site_key, attachment_id):
    problem = get_object_or_404(Problem, problemsite__url_key=site_key)
    attachment = get_object_or_404(ProblemAttachment, id=attachment_id)
    if attachment.problem.id != problem.id:
        raise PermissionDenied
    return stream_file(attachment.content, attachment.download_name)


def problemset_add_to_contest_view(request, site_key):
    problem_name = request.GET.get('problem_name')
    if not problem_name:
        raise Http404
    administered = administered_contests(request)
    administered = sorted(administered, key=lambda x: x.creation_date, reverse=True)
    navbar_links = navbar_links_registry.template_context(request)
    problemset_tabs = generate_problemset_tabs(request)
    problemset_tabs.append(
        {
            'name': _('Add to contest'),
            'url': reverse('problemset_add_to_contest', kwargs={'site_key': site_key}),
        }
    )
    return TemplateResponse(
        request,
        'problems/problemset/select-contest.html',
        {
            'site_key': site_key,
            'administered_contests': administered,
            'problem_name': problem_name,
            'navbar_links': navbar_links,
            'problemset_tabs': problemset_tabs,
        },
    )


def get_report_HTML_view(request, submission_id):
    submission = get_object_or_404(Submission, id=submission_id)
    controller = submission.problem_instance.controller
    if not controller.filter_my_visible_submissions(
        request, Submission.objects.filter(id=submission_id)
    ).exists():
        raise Http404
    reports = ''
    queryset = SubmissionReport.objects.filter(submission=submission).prefetch_related(
        'scorereport_set'
    )
    for report in controller.filter_visible_reports(
        request, submission, queryset.filter(status='ACTIVE')
    ):
        reports += controller.render_report(request, report)

    if not reports:
        reports = _(u"Reports are not available now (ಥ ﹏ ಥ)")
        reports = mark_safe(u'<center>' + force_text(reports) + u'</center>')
    return HttpResponse(reports)


@transaction.non_atomic_requests
def problemset_add_or_update_problem_view(request):
    if not can_add_to_problemset(request):
        if request.contest:
            url = (
                reverse('add_or_update_problem')
                + '?'
                + six.moves.urllib.parse.urlencode(request.GET.dict())
            )
            return safe_redirect(request, url)
        raise PermissionDenied

    return add_or_update_problem(
        request, None, 'problems/problemset/add-or-update.html'
    )


def task_archive_view(request):
    origin_tags = (
        OriginTag.objects.all().prefetch_related('localizations').order_by('name')
    )

    navbar_links = navbar_links_registry.template_context(request)
    return TemplateResponse(
        request,
        'problems/task-archive.html',
        {
            'navbar_links': navbar_links,
            'origin_tags': origin_tags,
        },
    )


def _recursive_group_problems(problems, result_info, categories, div_id):
    if not categories:
        problems.sort(key=attrgetter('short_name'))
        results = [result_info[problem] for problem in problems]
        percentages = [
            100.0 * result['score'] / result['max_score'] if result['exists'] else 0.0
            for result in results
        ]

        if len(problems) > 0:
            total_percent_solved = sum(percentages) / len(problems)
        else:
            total_percent_solved = 0.0

        return {
            'div_id': 'problems-' + div_id,
            'subnodes': {},
            'problem_info': zip(problems, results),
            'progress_percentage': total_percent_solved,
            'progress_percentage_rounded': round(total_percent_solved, 1),
            'attempted': any(result['exists'] for result in results),
        }

    node = {
        'div_id': div_id,
        'subnodes': {},
        'problem_info': [],
        'progress_percentage': 0.0,
        'progress_percentage_rounded': 0.0,
        'attempted': False,
    }

    category = categories[0]
    iter = groupby(
        problems, key=lambda problem: get_prefetched_value(problem, category)
    )

    total_percentage = 0

    for value, group in iter:
        child_id = div_id + '-' + str(value.value)
        child_problems = list(group)
        child = _recursive_group_problems(
            child_problems, result_info, categories[1:], child_id
        )
        node['subnodes'][value] = child
        if child['attempted'] == True:
            node['attempted'] = True
        total_percentage += child['progress_percentage'] * len(child_problems)

    if len(problems) > 0:
        total_percentage /= len(problems)
    else:
        total_percentage = 0.0

    node['progress_percentage'] = total_percentage
    node['progress_percentage_rounded'] = round(total_percentage, 1)

    return node


def _get_results_info(request, problems):
    if request.user.is_authenticated == False:
        return {problem: {'exists': False} for problem in problems}

    main_instances = [problem.main_problem_instance for problem in problems]

    user_results = (
        UserResultForProblem.objects.filter(
            user=request.user,
            problem_instance__in=main_instances,
            submission_report__isnull=False,
        )
        .select_related('problem_instance', 'submission_report__submission')
        .prefetch_related('submission_report__scorereport_set')
    )

    user_result_for_instance = {}

    for result in user_results:
        user_result_for_instance[result.problem_instance] = result

    results_info = {}

    for problem in problems:
        result = user_result_for_instance.get(problem.main_problem_instance)
        if result is None:
            results_info[problem] = {'exists': False}
        else:
            try:
                score = result.score
                max_score = result.submission_report.score_report.max_score

                def result_info(_int_score, _int_max_score):
                    return {
                        'exists': True,
                        'score': _int_score,
                        'max_score': _int_max_score,
                        'submission_url': reverse(
                            'submission',
                            kwargs={
                                'submission_id': result.submission_report.submission.id
                            },
                        ),
                    }

                if score is None and max_score is None:
                    if (
                        result.status is None
                        or result.status == '?'
                        or result.status == 'INI_OK'
                        or result.status == 'INI_ERR'
                    ):
                        results_info[problem] = {'exists': False}
                    elif result.status == 'OK':
                        results_info[problem] = result_info(1, 1)
                    else:
                        results_info[problem] = result_info(0, 1)
                else:
                    results_info[problem] = result_info(
                        score.to_int(), max_score.to_int()
                    )

            except AttributeError:
                results_info[problem] = {'exists': False}

    return results_info


def _filter_problems_prefetched(problems, filter_multivaluedict):
    result = []

    for problem in problems:
        remaining_filters = set(
            _filter_neutral_get_params(filter_multivaluedict.keys())
        )

        for infovalue in problem.origininfovalue_set.all():
            category = infovalue.category
            value = infovalue.value

            # Check if this info-value combo violates any filters
            if category.name in remaining_filters:
                remaining_filters.remove(category.name)
                allowed_values = filter_multivaluedict.getlist(category.name)
                if value not in allowed_values:
                    break
        else:
            # If filtering info=value don't include problems with no value
            if not remaining_filters:
                result.append(problem)

    return result


def task_archive_tag_view(request, origin_tag):
    origin_tag = OriginTag.objects.filter(name=origin_tag).prefetch_related(
        'localizations',
        'info_categories__localizations',
        'info_categories__parent_tag__localizations',
        'info_categories__values__localizations',
        'info_categories__values__parent_tag__localizations',
    )
    origin_tag = get_object_or_404(origin_tag)

    categories = origin_tag.info_categories.all()
    # We use getparams for filtering by OriginInfo - make sure they are valid
    for getparam in _filter_neutral_get_params(request.GET.keys()):
        if not categories.filter(name=getparam).exists():
            raise Http404

    problems = (
        origin_tag.problems.all()
        .select_related('problemsite', 'main_problem_instance')
        .prefetch_related(
            'origininfovalue_set__localizations', 'origininfovalue_set__category'
        )
    )
    problems = _filter_problems_prefetched(problems, request.GET)

    # We want to achieve something like Django's regroup, but with dynamic keys:

    # 1. Don't use categories with Null order for grouping
    categories = sorted(
        [cat for cat in categories if cat.order], key=lambda cat: cat.order
    )

    # 2. Stable sort the problem list by each category in reverse order.
    #    This gives the correct order for the final grouping.
    for cat in categories[::-1]:
        problems.sort(key=lambda problem: get_prefetched_value(problem, cat).order)

    # 3. Now we can recursively group the problem list by each category.

    user_results = _get_results_info(request, problems)

    problems_root_node = _recursive_group_problems(
        problems, user_results, categories, 'problemgroups'
    )

    navbar_links = navbar_links_registry.template_context(request)
    return TemplateResponse(
        request,
        'problems/task-archive-tag.html',
        {
            'origin_tag': origin_tag,
            'problems': problems_root_node,
            'navbar_links': navbar_links,
        },
    )


def _filter_neutral_get_params(keys):
    neutral_get_params = {"fbclid"}
    for param in keys:
        if param not in neutral_get_params:
            yield param


def model_solutions_view(request, problem_instance_id):
    problem_instance = get_object_or_404(ProblemInstance, id=problem_instance_id)
    if not can_admin_problem_instance(request, problem_instance):
        raise PermissionDenied
    context = generate_model_solutions_context(request, problem_instance)

    return TemplateResponse(request, 'programs/admin/model_solutions.html', context)


def rejudge_model_solutions_view(request, problem_instance_id):
    problem_instance = get_object_or_404(ProblemInstance, id=problem_instance_id)
    contest = problem_instance.contest
    if not request.user.has_perm('contests.contest_admin', contest):
        raise PermissionDenied
    ModelSolution.objects.recreate_model_submissions(problem_instance)
    messages.info(request, _("Model solutions sent for evaluation."))
    return redirect('model_solutions', problem_instance.id)


@enforce_condition(not_anonymous)
def get_last_submissions(request):
    queryset = (
        Submission.objects.filter(user=request.user)
        .order_by('-date')
        .select_related(
            'problem_instance',
            'problem_instance__contest',
            'problem_instance__round',
            'problem_instance__problem',
        )[:5]
    )
    submissions = [submission_template_context(request, s) for s in queryset]
    return TemplateResponse(
        request,
        "contests/my_submissions_table.html",
        {'submissions': submissions, 'show_scores': True, 'hide_reports': True},
    )


def _get_tag_hints(request, tags_manager):
    substr = request.GET.get('substr', '')
    if len(substr) < 2:
        raise Http404
    num_hints = getattr(settings, 'NUM_HINTS', 10)
    queryset_tags = tags_manager.filter(name__icontains=substr)[:num_hints]

    return [tag.name for tag in queryset_tags]


def uniquefy_tag_names(fn):
    @wraps(fn)
    def decorated(*args, **kwargs):
        return list(set(tag_name for tag_name in fn(*args, **kwargs)))

    return decorated


@jsonify
def get_difficultytag_hints_view(request):
    return _get_tag_hints(request, DifficultyTag.objects)


@jsonify
@uniquefy_tag_names
def get_algorithmtag_hints_view(request):
    substr = request.GET.get('substr', '')
    if len(substr) < 2:
        raise Http404
    num_hints = getattr(settings, 'NUM_HINTS', 10)
    if num_hints > 1:
        num_hints //= 2

    results = []
    results.extend(
        [
            tag.name
            for tag in AlgorithmTag.objects.filter(name__icontains=substr)[:num_hints]
        ]
    )
    results.extend(
        [
            tag.full_name
            for tag in AlgorithmTagLocalization.objects.filter(
                full_name__icontains=substr
            )[:num_hints]
        ]
    )

    return results


@jsonify
def get_tag_hints_view(request):
    return _get_tag_hints(request, Tag.objects)


def _uniquefy(key, list_of_dicts):
    uniquefied = {item[key]: item for item in list_of_dicts}
    return uniquefied.values()


def uniquefy(key):
    def decorator(fn):
        @wraps(fn)
        def decorated(*args, **kwargs):
            return _uniquefy(key, fn(*args, **kwargs))

        return decorated

    return decorator


def get_origintag_category_hints(origintag):
    origintag = OriginTag.objects.get(name=origintag)
    return [
        {
            'trigger': 'category-menu',
            'name': u'{} - {}'.format(origintag.full_name, category.full_name),
            'category': _("Origin Tags"),
            'search_name': origintag.full_name,  # Avoids breaking the typeahead
            'value': category.name,
        }
        for category in origintag.info_categories.all()
    ]


@uniquefy('value')
def get_origininfovalue_hints(query):
    query_prefix = query.split('_')

    return (
        [
            {
                'trigger': 'origininfo',
                'name': oiv.full_name,
                'category': _("Origin Tags"),
                'prefix': 'origin',
                'value': oiv.name,
            }
            for oiv in OriginInfoValue.objects.filter(value__istartswith=query)
        ]
        + [
            {
                'trigger': 'origininfo',
                'name': oivl.origin_info_value.full_name,
                'category': _("Origin Tags"),
                'prefix': 'origin',
                'value': oivl.origin_info_value.name,
            }
            for oivl in OriginInfoValueLocalization.objects.filter(
                full_value__istartswith=query
            )
        ]
        + (
            [
                {
                    'trigger': 'origininfo',
                    'name': oiv.full_name,
                    'category': _("Origin Tags"),
                    'prefix': 'origin',
                    'value': oiv.name,
                }
                for oiv in OriginInfoValue.objects.filter(
                    parent_tag__name__iexact=query_prefix[0],
                    value__istartswith=query_prefix[1],
                )
            ]
            if len(query_prefix) == 2
            else []
        )
    )


def get_origintag_hints(query):
    res = _uniquefy(
        'name',
        [
            {
                'trigger': 'origintag-menu',
                'name': otl.origin_tag.full_name,
                'category': _("Origin Tags"),
                'prefix': 'origin',
                'value': otl.origin_tag.name,
            }
            for otl in OriginTagLocalization.objects.filter(full_name__icontains=query)
        ],
    )

    res = list(res)
    if len(res) == 1:
        res[0]['trigger'] = 'origintag'
        res += get_origintag_category_hints(res[0]['value'])

    return res


@uniquefy('name')
def get_tag_hints(query):
    prefixes = ('tag', 'algorithm', 'difficulty')
    categories = (
        _("Tags"),
        _("Algorithm Tags"),
        _("Difficulty Tags"),
    )
    models = (Tag, AlgorithmTag, DifficultyTag)

    results = []
    for prefix, category, model in zip(prefixes, categories, models):
        results.extend(
            [
                {
                    'name': tag.name,
                    'category': category,
                    'prefix': prefix,
                }
                for tag in model.objects.filter(name__icontains=query)
            ]
        )
    results.extend(
        [
            {
                'name': tag.full_name,
                'category': _("Algorithm Tags"),
                'prefix': 'algorithm',
            }
            for tag in AlgorithmTagLocalization.objects.filter(
                full_name__icontains=query
            )
        ]
    )

    return results


@uniquefy('name')
def get_problem_hints(query, view_type, user):
    problems = Problem.objects.filter(
        Q(ascii_name__icontains=query) | Q(short_name__icontains=query),
        problemsite__isnull=False,
    )
    if view_type == 'public':
        problems = problems.filter(visibility=Problem.VISIBILITY_PUBLIC)
    elif view_type == 'my':
        problems = problems.filter(author=user)
    elif view_type != 'all':
        raise Http404

    # Limit the number of matches sent
    return [
        {
            'trigger': 'problem',
            'name': problem.name,
            'category': _("Problems"),
        }
        for problem in problems[: getattr(settings, 'NUM_HINTS', 10)]
    ]


@jsonify
def get_search_hints_view(request, view_type):
    """Search hints are JSON objects with the following fields:

    name - name displayed in the dropdown box
    category (optional) - category for grouping in the dropdown box

    prefix (only search tags) - GET param key and prefix for the search tag

    search_name (optional) - to be passed to the search box instead of `name`
    value (optional) - to be used as a GET param value instead of `name`

    trigger (optional) - special trigger for the internal logic of the
                         typeahead script, see `init_search_selection`
    """
    if view_type == 'all' and not request.user.is_superuser:
        raise PermissionDenied
    query = unidecode(request.GET.get('q', ''))

    result = []
    result.extend(list(get_problem_hints(query, view_type, request.user)))
    result.extend(get_tag_hints(query))
    result.extend(get_origintag_hints(query))
    result.extend(get_origininfovalue_hints(query))

    # Convert category names in results from lazy translation to strings
    # Since jsonify throws error if given lazy translation objects
    for tag in result:
        if six.PY2:
            tag['category'] = tag['category'].encode('utf-8')
        else:
            tag['category'] = str(tag['category'])
    return result


@jsonify
def get_origininfocategory_hints_view(request):
    tag = get_object_or_404(
        OriginTagLocalization, language=get_language(), full_name=request.GET.get('q')
    ).origin_tag
    category = get_object_or_404(
        OriginInfoCategory, parent_tag=tag, name=request.GET.get('category')
    )
    if not category:
        raise Http404

    return [
        {
            'trigger': 'origininfo',
            'name': u'{} {}'.format(category.parent_tag.full_name, val.full_value),
            'prefix': 'origin',
            'value': val.name,
        }
        for val in category.values.all()
    ]


@jsonify
@uniquefy_tag_names
def get_tag_proposal_hints_view(request):
    query = request.GET.get('query', '')
    base_hints = [
        tag.name for tag in AlgorithmTag.objects.filter(name__istartswith=query)
    ]
    localized_hints = [
        tag.full_name
        for tag in AlgorithmTagLocalization.objects.filter(full_name__istartswith=query)
    ]

    return base_hints + localized_hints


@jsonify
@uniquefy_tag_names
def get_tag_label_view(request):
    name = request.GET.get('name', '')
    base_tags = AlgorithmTag.objects.filter(name=name)
    localized_tags = AlgorithmTagLocalization.objects.filter(name=name)
    proposed = request.GET.get('proposed', -1)

    if proposed != '-1' or not (base_tags or localized_tags):
        raise Http404

    return [tag.name for tag in base_tags] + [tag.full_name for tag in localized_tags]


def save_proposals_view(request):
    if request.method == 'POST':
        tags = request.POST.getlist('tags[]')
        user = (
            User.objects.all().filter(username=request.POST.get('user', None)).first()
        )
        problem = (
            Problem.objects.all().filter(pk=request.POST.get('problem', None)).first()
        )

        if not user or not problem:
            return None

        for tag in tags:
            tag_proposal = AlgorithmTag.objects.filter(name=tag)
            if tag_proposal.exists():
                tag_proposal = tag_proposal.first()
            else:
                tag_proposal = (
                    AlgorithmTagLocalization.objects.filter(full_name=tag)
                    .first()
                    .algorithm_tag
                )

            proposal = AlgorithmTagProposal(
                problem=problem,
                tag=tag_proposal,
                user=user,
            )
            proposal.save()

        if request.POST.get('difficulty', None):
            proposal = DifficultyProposal(
                problem=problem, difficulty=request.POST['difficulty'], user=user
            )
            proposal.save()

        return HttpResponse('success\n' + str(tags))


def download_problem_package_file_view(request, package_id, file_name):
    package = _get_package(request, package_id, 'contests.contest_admin')
    archive = Archive(package.package_file)
    dir_path = tempfile.mkdtemp(dir=tempfile.gettempdir())
    try:
        archive.extract(to_path=dir_path)
        filepath = os.path.join(dir_path, file_name)
        if os.path.isfile(filepath):
            return stream_file(
                File(open(filepath)), os.path.basename(os.path.normpath(file_name))
            )
    finally:
        shutil.rmtree(dir_path)
