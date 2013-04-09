$(function(){
    var menu_ul = '#menu_div .nav-list';
    var last_li = menu_ul + ' > li:nth-last-of-type(3)';
    var submenu = menu_ul + ' .nav-dropdown-submenu';
    var submenu_ul = submenu + ' > ul';
    var submenu_first_li = submenu_ul + ' > li:first';
    var submenu_divider = '#nav-dropdown-divider';
    var footer = $('.body-with-menu footer');


    var hide_first = function() {
        if ($(submenu_ul).is(':empty')) {
            $(submenu_divider).show();
            $(submenu).show();
        }

        $(last_li).prependTo(submenu_ul);
    };

    var show_first = function() {
        $(submenu_first_li).insertBefore(submenu_divider);
        if ($(submenu_ul).is(':empty')) {
            $(submenu_divider).hide();
            $(submenu).hide();
        }
    };

    var recalculate_menu = function() {
        var ul = $(menu_ul);
        var cut_y = footer.length ? footer.offset().top : $(window).height();

        while ($(submenu_first_li).length == 1
            && ul.offset().top + ul.height() < cut_y) {
            show_first();
        }

        while ($(last_li).length == 1
            && ul.offset().top + ul.height() > cut_y) {
            hide_first();
        }
    };

    recalculate_menu();
    $(window).resize(recalculate_menu);
});
