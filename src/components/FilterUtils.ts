import {
    Category,
    ClearplayCategories,
    ClearplayToVideoSkipCategoryMap,
    Filter,
    FilterSettings,
    Incident
} from "./FilterTypes";
import {uniqWith} from "lodash";

export type FilterOption = {
    selected: boolean
    incident: Incident
}

export interface ClearplayFilterGroup{
    category: Category;
    filters: FilterOption[]
}

export const formatFilterSettings = (filterSettings: FilterSettings): ClearplayFilterGroup[] => {
    return filterSettings.filterSettingsUI.category
        .map(category => ({
                category,
                filters: uniqWith(category.subcategory
                    .filter(subcategory => subcategory.incident != null)
                    .flatMap(subCategory => subCategory.incident!.map(incident => ({
                        incident: incident,
                        selected: true
                    })))
                    , (a, b) => a.incident.id === b.incident.id)
        }))
}


const convertToTimestamp = (tsInSeconds: number): string => {
    return new Date(tsInSeconds * 1000).toISOString().slice(11, 22)
}

export const convertToVideoSkip = (formattedFilters: ClearplayFilterGroup[], filters: Filter, offset: number): string => {
    const newFilters: string[] = [];

    formattedFilters.map(filterGroup => ({
        category: filterGroup.category,
        filters: filterGroup.filters.filter(filter => filter.selected)
    }))
        .filter(filterGroup => filterGroup.filters.length > 0)
        .forEach(filterGroup =>
            filterGroup.filters.forEach(incident => {
                const filter = filters.eventList.find(filter => filter.id === incident.incident.id)

                if (filter) {
                    const filterType = ClearplayToVideoSkipCategoryMap[filterGroup.category.desc]
                    newFilters.push(`${convertToTimestamp(filter.interrupt + offset)} --> ${convertToTimestamp(filter.resume + offset)}`
                        + `\n${filterType} 1 (${incident.incident.context})\n`)
                }
            }))

    return newFilters.join('\n')
}

export const convertToEDLFormat = (formattedFilters: ClearplayFilterGroup[], filters: Filter, offset: number): string => {
    const newFilters: string[] = [];

    formattedFilters.map(filterGroup => ({
        category: filterGroup.category,
        filters: filterGroup.filters.filter(filter => filter.selected)
    }))
        .filter(filterGroup => filterGroup.filters.length > 0)
        .forEach(filterGroup =>
            filterGroup.filters.forEach(incident => {
                const filter = filters.eventList.find(filter => filter.id === incident.incident.id)

                if (filter) {
                    const filterType = filterGroup.category.desc === ClearplayCategories.LANGUAGE ? 1 : 0
                    newFilters.push(`${filter.interrupt + offset} ${filter.resume + offset} ${filterType}`)
                }
            }))

    return newFilters.join('\n')
}
