def classFactory(iface):
    from .quick_print_export import QuickPrintExportPlugin
    return QuickPrintExportPlugin(iface)
